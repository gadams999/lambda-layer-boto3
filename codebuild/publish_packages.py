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

# Environment variables set by CodeBuild
table_name = os.environ['VERSION_TABLE']
# build_dir = os.environ['CODEBUILD_SRC_DIR']

class Package:
    '''Lambda python package class'''

    docker_runtime_map = {
        'python27': 'python2.7',
        'python36': 'python3.6',
        'python37': 'python3.7'
    }
    docker_base_image = 'lambci/lambda:build-'

    def __init__(self, package_list, table, table_region, lambda_regions):
        self.package_list = package_list
        self.is_current = True

        # Load MASTER record for package_list
        try:
            self.db = boto3.resource('dynamodb', region_name=table_region)
            self.table = self.db.Table(table)
            r = self.table.query(
                KeyConditionExpression=
                    Key('PK').eq(package_list) &
                    Key('SK').eq('MASTER')
            )
        except ClientError as e:
            logging.error('Invalid DynamoDB configuration, %s' % e)

        # Parse MASTER record
        try:
            # All attributes need to be present in record
            self.runtimes = r['Items'][0]['Runtimes']
            if 'PublishedPackageVersion' in r['Items'][0]:
                self.published_package_version = r['Items'][0]['PublishedPackageVersion']
            else:
                self.published_package_version = '0'
            if r['Items'][0]['Regions'] == '*':
                self.regions = lambda_regions
            else:
                self.regions = r['Items'][0]['Regions']
            if r['Items'][0]['PackageVersions'] == []:
                self.package_versions = []
                for i in package_list.split(','):
                    self.package_versions.append({'package': i, 'version': ''})
                self.is_current = False
            else:
                # Create package_versions as dictionary of package and versions
                self.package_versions = r['Items'][0]['PackageVersions']
        except Exception as e:
            logging.error("Cannot read MASTER record for package %s, error: %s",
                package_list, e)

        # Complete object variables from other sources
        # Get Pypi versions of the package_list
        self.pkg_pypi_versions = self._get_pypi(package_list)
        # Check if any package on Pypi is newer
        if self._check_for_newer_version(self.package_versions, self.pkg_pypi_versions):
            logging.info('Packages %s are current, no updates needed' % self.package_list)
            self.is_current = False
        else:
            logging.info('Packages %s are NOT current, updates needed' % self.package_list)
            self.is_current = True


    def Publish(self):
        '''Create layer from docker then publish to target regions'''

        if self.is_current:
            logging.info('%s is current, no publishing needed' % self.package_list)
            return False
        else:
            # Build package list from specific Pypi obtained versions
            zipfiles = self._build_for_runtimes(
                self.package_list,
                self.runtimes,
                self.pkg_pypi_versions
            )
            self._publish_lambda_layer(zipfiles)

    def _get_pypi(self, packages):
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


    def _check_for_newer_version(self, cur_ver, pypi_ver):
        '''Check if dict value(s) in cur_ver are older than dict values in pypi_ver,
        any one older value indicates they should be processed
        
        cur_ver is [{"package": "pkg", "version": "ver"}, ...]
        pypi_ver is [{"package": "pkg", "version": "ver"}, ...] 
        '''

        result = {}
        new_version_found = False
        pypi_dict = {i['package']: i['version'] for i in pypi_ver}
        cur_dict = {i['package']: i['version'] for i in cur_ver}
        # Return pypi values if this package has never been published
        if cur_ver == []:
            for i in pypi_dict:
                result[i] = pypi_dict[i]
            return result

        # Check all versions and if any pypi are newer, process as new
        for i in cur_dict:
            result[i] = pypi_dict[i]
            if parse_version(pypi_dict[i]) > parse_version(cur_dict[i]): 
                new_version_found = True
        return result if new_version_found else False


    def _build_for_runtimes(self, package_list, runtimes, package_versions):
        '''Create zipfiles of latest packages for each runtime using docker lambci
        runtimes = "runtime1,runtime2..."
        package_list = "pkg1,pkg2..."
        package_versions={"package": "name", "version"}'''
        logger.info('Building for %s, %s', runtimes, package_versions)
        result = {}
        client = docker.from_env()
        for runtime in runtimes.split(','):
            runtime_folder = Path('/tmp/{}-{}/python/lib/{}/site-packages'.format(
                package_list,
                runtime,
                self.docker_runtime_map[runtime]
            ))
            logger.info('Creating temporary build directory: %s', runtime_folder)
            runtime_folder.mkdir(parents=True)
            logger.info("Executing docker container %s%s to install package(s) %s",
                self.docker_base_image,
                self.docker_runtime_map[runtime],
                package_list
            )
            try:
                pip_command = ' '.join(['{}=={}'.format(d['package'], d['version']) for d in package_versions])
                client.containers.run(
                    self.docker_base_image + self.docker_runtime_map[runtime],
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
                str(Path('/tmp/{}-{}'.format(package_list, runtime))),
                'zip',
                str(Path('/tmp/{}-{}'.format(package_list, runtime)))
            )
            result[package_list+'-'+runtime] = str(Path('/tmp/{}-{}.zip'.format(package_list, runtime)))
            shutil.rmtree(str(Path('/tmp/{}-{}'.format(package_list, runtime))))

        # Return runtimes and file names in Path format
        return result


    def _publish_lambda_layer(self, zipfiles):
        '''Create layer from zipfiles'''
        desc = 'Packages: '+','.join(['{}=={}'.format(d['package'], d['version']) for d in self.pkg_pypi_versions])
        if len(self.package_list.split(',')) == 1:
            version = self.pkg_pypi_versions[0]['version']
        else:
            version = (str(int(self.published_package_version) + 1))
        # For each region, package and runtime create a layer and make a DDB entry
        for region in self.regions.split(','):
            lambda_client = boto3.client('lambda', region_name=region)
            for runtime in self.runtimes.split(','):
                # Convert pkg1,pkg2 to pkg1_pgk2 for layer name
                layer_name = '{}-{}'.format(
                    self.package_list.replace(',', '_'),
                    runtime
                )
                zip_name = '{}-{}'.format(self.package_list, runtime)
                try:
                    response = lambda_client.publish_layer_version(
                        LayerName=layer_name,
                        Description=desc,
                        Content={
                            'ZipFile': open(zipfiles[zip_name], 'rb').read()
                        },
                        CompatibleRuntimes=[self.docker_runtime_map[runtime]],
                        LicenseInfo='Apache-2.0'
                    )
                    version_num = response['Version']
                    arn = response['LayerVersionArn']
                    response = lambda_client.add_layer_version_permission(
                        LayerName=layer_name,
                        VersionNumber=version_num,
                        StatementId='FullPublicAccess',
                        Action='lambda:GetLayerVersion',
                        Principal='*'
                    )
                    logger.info('Created layer: %s in %s, permissions set',
                        layer_name, region)
                    
                    # create DDB record for published layer
                    self.table.put_item(
                        Item={
                            'PK': self.package_list,
                            'SK': arn,
                            'Runtime': runtime,
                            'PackageVersion': version
                        }
                    )

                except ClientError as e:
                    logger.error('Error creating new layer for %s:%s, error: %s',
                        region, runtime, e.response['Error']['Message'])
                    sys.exit(1)
        # Update DDB record (and self) with latest version
        self.table.update_item(
            Key={
                'PK': self.package_list,
                'SK': 'MASTER'
            },
            UpdateExpression='set PublishedPackageVersion = :ppv, PackageVersions = :packver',
            ExpressionAttributeValues={
                ':ppv': version,
                ':packver': self.pkg_pypi_versions
            },
            ReturnValues='UPDATED_NEW'
        )

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
    lambda_regions = ','.join(lambda_regions)
    logger.info('Lambda available in the following regions: %s' % lambda_regions)
    return lambda_regions


def main():
    '''Main entry point'''

    # Collect all regions Lambda is available
    #wildcard_regions = lambda_regions()
    wildcard_regions = 'us-east-1,us-west-2'
    # for pkg in package_records:
    #     logger.info('Starting build process for package group: %s' % pkg['PackageList'])

    dynamodb = boto3.resource('dynamodb', region_name=os.environ['AWS_DEFAULT_REGION'])
    table = dynamodb.Table(os.environ['VERSION_TABLE'])

    response = table.query(
        IndexName='GSI1',
        KeyConditionExpression=Key('GSIPK').eq('PACKAGE')
    )
    for pkg in response['Items']:
        logger.info('Starting build process for package group: %s' % pkg['PK'])
        package = Package(
            package_list=pkg['PK'],
            table=os.environ['VERSION_TABLE'],
            table_region=os.environ['AWS_DEFAULT_REGION'],
            lambda_regions=wildcard_regions
        )
        package.Publish()
    logger.info('*** Completed build process ***')


if __name__ == "__main__":
    main()

