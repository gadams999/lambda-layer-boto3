#!/usr/bin/env python3

import requests
import sys
import os
import logging
import json
import time
from pkg_resources import parse_version
from jinja2 import Template
from datetime import datetime
import boto3
from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError

logger = logging.getLogger()
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
logger.addHandler(handler)

# Constants
MASTER_REGION='MASTER'
MASTER_TIMESTAMP='0'

version_to_process = os.environ['PKG_VERSION']
table_name = os.environ['VERSION_TABLE']
build_dir = os.environ['CODEBUILD_SRC_DIR']
compatible_runtimes = {
    'python27': ['python2.7'],
    'python36': ['python3.6'],
    'python37': ['python3.7'],
    'combined': ['python2.7', 'python3.6', 'python3.7']
}

# Global resource used by various functions
db_resource = boto3.resource('dynamodb')

def lambda_regions():
    '''Return list of regions that support AWS Lambda. Note, does not include
       non-public regions such as GovCloud or China'''

    ec2 = boto3.client('ec2')
    regions = [region['RegionName'] for region in ec2.describe_regions()['Regions']]

    lambda_regions = []
    for region in regions:
        lambda_client = boto3.client('lambda', region_name=region)
        try:
            lambda_client.get_account_settings()
            lambda_regions.append(region)
        except ClientError as e:
            logging.info(
                'region: %s does not support Lambda, error message was: %s' %
                (region, e)
            )
            continue
    return lambda_regions  


def latest_db_version():
    '''Returns latest published version from DynamoDB table or None
       if no value exists'''
    
    # Magic values for the record with most recently processed version
    region = MASTER_REGION
    timestamp = MASTER_TIMESTAMP
    try:
        response = table.get_item(
            Key={
                'region': region,
                'timestamp': timestamp
            }
        )
    except ClientError as e:
        logger.error("%s", e.response['Error']['Message'])   
    else:
        try:
            version = response['Item']['package_version']
            return version
        except KeyError:
            return None


def update_db_version(version):
    '''Updates DynamoDB tracking record with the provided version'''
    table = db_resource.Table(table_name)
    table.put_item(
        Item={
            'region': MASTER_REGION,
            'timestamp': MASTER_TIMESTAMP,
            'package_version': version
        }
    )


def publish_layers(regions, compatible_runtimes):
    '''Main function to process runtimes and publish new layer and/or version'''

    table = db_resource.Table(table_name)
    for region in regions:
        lambda_client = boto3.client('lambda', region_name=region)
        for runtime in compatible_runtimes:
            # create layer and DDB record
            try:
                response = lambda_client.publish_layer_version(
                    LayerName='boto3-{}'.format(runtime),
                    Description='boto3/botocore version: {}'.format(
                        version_to_process
                    ),
                    Content={
                        'ZipFile': open('/tmp/boto3-{}-{}.zip'.format(
                            runtime, version_to_process), 'rb').read()
                    },
                    CompatibleRuntimes=compatible_runtimes[runtime],
                    LicenseInfo='Apache-2.0'
                )
                version_num = response['Version']
                arn = response['LayerVersionArn']
                response = lambda_client.add_layer_version_permission(
                    LayerName='boto3-{}'.format(runtime),
                    VersionNumber=version_num,
                    StatementId='FullPublicAccess',
                    Action='lambda:GetLayerVersion',
                    Principal='*'
                )
                logger.info('Created layer: %s in %s, permissions set',
                    runtime, region)
            except ClientError as e:
                logger.error('Error creating new layer for %s:%s, error: %s',
                    region, runtime, e.response['Error']['Message'])
                sys.exit(1)
            else:
                # layer has been published as access granted, create DB entry
                table.put_item(
                    Item={
                        'region': region,
                        'timestamp': str(int(time.time())),
                        'package_version': version_to_process,
                        'runtimes': ','.join(compatible_runtimes[runtime]),
                        'arn': arn
                    }
                )
                print('ddb entry made for layer')


# Collect all regions Lambda is available
regions = lambda_regions()

regions = ['us-east-1', 'us-west-2']
table = db_resource.Table(table_name)

last_version_processed = latest_db_version()
if last_version_processed:
    # Tracking record exists
    if parse_version(version_to_process) > parse_version(last_version_processed):
        print ('new version to process, database is at: {}, pypi reporting: {}'.format(
            last_version_processed, version_to_process)
        )
        publish_layers(regions, compatible_runtimes)
        update_db_version(version_to_process)
    else:
        print('pypi version {} has already been processed, exiting'.format(version_to_process))
        logger.info('pypi version %s has already been processed, exiting', version_to_process)
else:
    # Database does not have tracking record, publish and create
    publish_layers(regions, compatible_runtimes)
    update_db_version(version_to_process)

# All layers have been updated, now generate new README files and commit to repo

# TODO - place into function once tested
table_list = []
for region in regions:
    r = table.query(
        IndexName='versionGSI',
        KeyConditionExpression=Key('region').eq(region) & 
        Key('package_version').eq(version_to_process)
    )
    for i in sorted(r['Items'], key=lambda kv: (len(kv['runtimes']), kv['runtimes'])):
        table_list.append({
            'region': region,
            'version': i['runtimes'],
            'arn': i['arn'],
            'datetime': datetime.utcfromtimestamp(int(i['timestamp'])).strftime('%Y-%m-%d %H:%M:%S')
        })

with open(build_dir + '/codebuild/readme.template', 'r') as f:
    template = Template(f.read())

readme_md = template.render(
    package_version=version_to_process,
    table_list=table_list
)
with open(build_dir + '/README.md', 'w') as f:
    f.write(readme_md)

# build:
#   README.md with updates for processed version and latest table of region, version, arn and other jinja entries
#   VERSION - JSON formatted file with the above details
# clone repo with token, copy over new files, commit, push



