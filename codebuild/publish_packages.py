#!/usr/bin/env python3

import requests
import sys
import os
import logging
import json
import time
import docker
import shutil
import zipfile
from pkg_resources import parse_version
from functools import lru_cache
from datetime import datetime
from pathlib import Path
import boto3
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
# handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# Constants
docker_runtime_map = {
    'python27': 'python2.7',
    'python36': 'python3.6',
    'python37': 'python3.7'
}
docker_base_image = 'lambci/lambda:build-'

# Environment variables set by CodeBuild
table_name = os.environ['VERSION_TABLE']
# build_dir = os.environ['CODEBUILD_SRC_DIR']

# Global resource used by various functions
db_resource = boto3.resource('dynamodb')

def lambda_regions():
    '''Return list of regions that support AWS Lambda. Note, does not include
       non-public regions such as GovCloud or China'''

    logger.info('Starting query of where Lambda is available')
    ec2 = boto3.client('ec2')
    regions = [region['RegionName'] for region in ec2.describe_regions()['Regions']]

    lambda_regions = []
    for region in regions:
        lambda_client = boto3.client('lambda', region_name=region)
        try:
            lambda_client.get_account_settings()
            lambda_regions.append(region)
        except ClientError as e:
            logger.info(
                'region: %s does not support Lambda, error message was: %s' %
                (region, e)
            )
            continue
    logger.info('Lambda available in the following regions: %s' % lambda_regions)
    return lambda_regions  


def package_list(table):
    '''Return list of packages and details to process from DynamoDB table'''

    r = table.query(
        KeyConditionExpression=Key('Arn').eq('MASTER')
    )
    if r['Count'] == 0:
        return []
    else:
        return r['Items']


@lru_cache(maxsize=100)
def get_pypi(packages):
    '''Returns latest Pypi published version of comma separated packages
       as a list of dictionaries'''
    response = []
    logger.info('Querying Pypi for package group: %s' % packages)
    try:
        for pkg in packages.split(','):
            url = "https://pypi.python.org/pypi/{}/json".format(pkg)
            response.append({
                'package': pkg,
                'version': str(sorted(requests.get(url).json()["releases"], key=parse_version)[-1:][0])
            })
    except Exception as e:
        logging.error('general error %s' % e)
        sys.exit(1)
    return response


def package_version(table, pkgs, pypi_versions):
    '''Create single version number for package or list of packages in
       package list provided. Multiple packages will be next integer from
       what is currently recorded DynamoDB table'''

    if len(pypi_versions) > 1:
        # Get most recent version of multiple packages - if NONE is returned this
        # has never been processed
        r = table.query(
            KeyConditionExpression=
                Key('Arn').eq('MASTER') &
                Key('PackageList').eq(pkgs)
        )
        if r['Items'][0]['PackageVersion'] == 'NONE':
            return '1'
        else:
            return int(r['Items'][0]['PackageVersion']) + 1
    else:
        return pypi_versions[0]['version']


def check_for_newer_version(cur_ver, pypi_ver):
    '''Check if dict value(s) in cur_ver are older than dict values in pypi_ver,
       any one older value indicates they should be processed
       
       cur_ver is string "pkg==ver,pkg==ver" or "none",
       pypi_ver is [{"package": "pkg", "version": "ver"}, ...] 
       '''

    result = {}
    new_version_found = False
    pypi_dict = {i['package']: i['version'] for i in pypi_ver}
    # Return pypi values if this package has never been published
    if cur_ver == 'none':
        for i in pypi_dict:
            result[i] = pypi_dict[i]
        return result

    # Determine if value from database is valid
    try:
        current = dict(s.split('==') for s in cur_ver.split(','))
    except ValueError:
        # List not valid, bad DDB record
        sys.exit(1)
    # Check all versions and if any pypi are newer, process as new
    for i in current:
        result[i] = pypi_dict[i]
        if parse_version(pypi_dict[i]) > parse_version(current[i]): 
            new_version_found = True
    return result if new_version_found else False


def build_for_runtimes(runtimes, pkgs):
    '''Create zipfiles of latest packages for each runtime using docker lambci
       runtimes = "runtime1,runtime2..."
       pkgs={"package_name", "version"}'''
    logger.info('Building for %s, %s', runtimes, pkgs)

    result = {}
    package_list = ','.join(['%s' % key for (key,value) in pkgs.items()])
    client = docker.from_env()
    # /tmp/python/lib/python2.7/site-packages
    for runtime in runtimes.split(','):
        runtime_folder = Path('/tmp/{}-{}/python/lib/{}/site-packages'.format(
            package_list,
            runtime,
            docker_runtime_map[runtime]
        ))
        logger.info('Creating temporary build directory: %s', runtime_folder)
        runtime_folder.mkdir(parents=True)
        logger.info("Executing docker container %s%s to install packages %s",
            docker_runtime_map,
            docker_runtime_map[runtime],
            package_list
        )
        try:
            pip_command = ' '.join(['%s==%s' % (key, value) for (key,value) in pkgs.items()])
            client.containers.run(
                docker_base_image + docker_runtime_map[runtime],
                '/bin/bash -c "pip install {} -t .; exit"'.format(pip_command),
                volumes={
                    runtime_folder: {'bind': '/var/task', 'mode': 'rw'}
                }
            )
        except Exception as e:
            logger.error('Serious error % s in creating runtime %s for %s, stopping build process',
                e, runtime, package_list
            )
            sys.exit(1)
        
        # Create zip file
        shutil.make_archive(
            Path('/tmp/{}-{}'.format(package_list, runtime)),
            'zip',
            Path('/tmp/{}-{}'.format(package_list, runtime))
        )
        result[package_list+'-'+runtime] = Path('/tmp/{}-{}.zip'.format(pkgs, runtime))
        shutil.rmtree(Path('/tmp/{}-{}'.format(pkgs, runtime)))

    # Return runtimes and file names in Path format
    return result


def publish_layers(table, regions, pkg_version, builds):
    '''For each region, publish layers and update DDB
       pkg_version contains each package and its version
       builds = each package-runtime and zipfile
    '''

    logger.info('Publishing for regions: {} and builds: {}'.format(
        regions, builds
    ))
    for region in regions.split(','):
        lambda_client = boto3.client('lambda', region_name=region)
        # Layername is the pkgs-runtime combination
        for layername in builds:
            response = lambda_client.publish_layer_version(
                LayerName=layername,
                Description='Python packages: {}, version: {}'.format(
                    layername.split('-')[0],
                    pkg_version
                ),
                Content={
                    'ZipFile': builds[layername].open('rb').read()
                },
                CompatibleRuntimes=layername.split('-')[1],
                LicenseInfo='Apache-2.0'
            )
            layer_version_num = response['Version']
            arn = response['LayerVersionArn']
            response = lambda_client.add_layer_version_permission(
                LayerName=layername,
                VersionNumber=layer_version_num,
                StatementId='FullPublicAccess',
                Action='lambda:GetLayerVersion',
                Principal='*'
            )
            logger.info('Created layer: %s in %s, permissions set',
                layername, region)
    return True



        # publish layer
        # create DDB record
    # return status object with pass or fail


#     table = db_resource.Table(table_name)
#     for region in regions:
#         lambda_client = boto3.client('lambda', region_name=region)
#         for runtime in compatible_runtimes:
#             # create layer and DDB record
#             try:
#                 response = lambda_client.publish_layer_version(
#                     LayerName='boto3-{}'.format(runtime),
#                     Description='boto3/botocore version: {}'.format(
#                         version_to_process
#                     ),
#                     Content={
#                         'ZipFile': open('/tmp/boto3-{}-{}.zip'.format(
#                             runtime, version_to_process), 'rb').read()
#                     },
#                     CompatibleRuntimes=compatible_runtimes[runtime],
#                     LicenseInfo='Apache-2.0'
#                 )
#                 version_num = response['Version']
#                 arn = response['LayerVersionArn']
#                 response = lambda_client.add_layer_version_permission(
#                     LayerName='boto3-{}'.format(runtime),
#                     VersionNumber=version_num,
#                     StatementId='FullPublicAccess',
#                     Action='lambda:GetLayerVersion',
#                     Principal='*'
#                 )
#                 logger.info('Created layer: %s in %s, permissions set',
#                     runtime, region)
#             except ClientError as e:
#                 logger.error('Error creating new layer for %s:%s, error: %s',
#                     region, runtime, e.response['Error']['Message'])
#                 sys.exit(1)
#             else:
#                 # layer has been published as access granted, create DB entry
#                 table.put_item(
#                     Item={
#                         'region': region,
#                         'timestamp': str(int(time.time())),
#                         'package_version': version_to_process,
#                         'runtimes': ','.join(compatible_runtimes[runtime]),
#                         'arn': arn
#                     }
#                 )
#                 print('ddb entry made for layer')


def main():
    '''Main entry point'''

    table = db_resource.Table(table_name)

    package_records = package_list(table)
    # Collect all regions Lambda is available
    wildcard_regions = lambda_regions()
    for pkg in package_records:
        logger.info('Starting build process for package group: %s' % pkg['PackageList'])
        pypi_versions = get_pypi(pkg['PackageList'])
        try:
            new_version_list = check_for_newer_version(pkg['PackageVersionList'], pypi_versions)
            # new_version_list contains new package(s) and version(s) or False if none to process
            if new_version_list:
                logger.info(
                    'New version of %s available, building site-packages for runtimes %s',
                    pkg['PackageList'],
                    pkg['Runtimes'])
                # breakpoint()
                build_result = build_for_runtimes(
                    pkg['Runtimes'],
                    new_version_list
                )
                logger.info('Built for: %s' % build_result)
                # Get or create a new package version for publish (either version if single
                # package, or incremental number if multiple packages)
                new_pkg_ver_num = package_version(table_name, pkg['PackageList'], pypi_versions)
                result = publish_layers(
                    table_name,
                    wildcard_regions if pkg['Regions'] == '*' else pkg['Regions'],
                    new_pkg_ver_num,
                    build_result
                )
            else:
                logger.info('Published layers are most current, skipping %s' % pkg['PackageList'])
        except KeyError as e:
            logging.error('Missing key %s for package %s', pkg, e)
        logger.info('Completed check, build, publish for packages: %s',
            pkg['PackageList'] in package_records
        )
    # for package in packages:
        # get pypi package(s) versions
        # if version is newer:
            # rumtimes = get_package_runtimes()
            # for runtime in runtimes:
                # docker commands to create directories / zip files
                # publish layer()
                



    # last_version_processed = latest_db_version()
    # if last_version_processed:
    #     # Tracking record exists
    #     if parse_version(version_to_process) > parse_version(last_version_processed):
    #         print ('new version to process, database is at: {}, pypi reporting: {}'.format(
    #             last_version_processed, version_to_process)
    #         )
    #         publish_layers(regions, compatible_runtimes)
    #         update_db_version(version_to_process)
    #     else:
    #         print('pypi version {} has already been processed, exiting'.format(version_to_process))
    #         logger.info('pypi version %s has already been processed, exiting', version_to_process)
    # else:
    #     # Database does not have tracking record, publish and create
    #     publish_layers(regions, compatible_runtimes)
    #     update_db_version(version_to_process)

    # All layers have been updated, now generate new README files and commit to repo

    # # TODO - place into function once tested
    # table_list = []
    # for region in regions:
    #     r = table.query(
    #         IndexName='versionGSI',
    #         KeyConditionExpression=Key('region').eq(region) & 
    #         Key('package_version').eq(version_to_process)
    #     )
    #     for i in sorted(r['Items'], key=lambda kv: (len(kv['runtimes']), kv['runtimes'])):
    #         table_list.append({
    #             'region': region,
    #             'version': i['runtimes'],
    #             'arn': i['arn'],
    #             'datetime': datetime.utcfromtimestamp(int(i['timestamp'])).strftime('%Y-%m-%d %H:%M:%S')
    #         })

    # with open(build_dir + '/codebuild/readme.template', 'r') as f:
    #     template = Template(f.read())

    # readme_md = template.render(
    #     package_version=version_to_process,
    #     table_list=table_list
    # )
    # with open(build_dir + '/README.md', 'w') as f:
    #     f.write(readme_md)

    # build:
    #   README.md with updates for processed version and latest table of region, version, arn and other jinja entries
    #   VERSION - JSON formatted file with the above details
    # clone repo with token, copy over new files, commit, push

if __name__ == "__main__":
    main()

