#!/usr/bin/env python3

import requests
import sys
import os
import logging
import json
import time
from pkg_resources import parse_version
import boto3
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
compatible_runtimes = {
    'python27': ['python2.7'],
    'python36': ['python3.6'],
    'python37': ['python3.7'],
    'combined': ['python2.7', 'python3.6', 'python3.7']
}

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


def latest_version():
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
        logger.error(e.response['Error']['Message'])   
    else:
        try:
            version = response['Item']['package_version']
            return version
        except KeyError:
            return None


def publish_layers(regions, compatible_runtimes):
    '''Main function to process runtimes and publish new layer and/or version'''

    table = db_resource.Table(table_name)
    for region in regions:
        print('Processing region: {}'.format(region))
        lambda_client = boto3.client('lambda', region_name=region)
        for runtime in compatible_runtimes:
            print('Processing runtime: {}'.format(runtime))
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
                print('created layer')
                version_num = response['Version']
                arn = response['LayerVersionArn']
                response = lambda_client.add_layer_version_permission(
                    LayerName='boto3-{}'.format(runtime),
                    VersionNumber=version_num,
                    StatementId='FullPublicAccess',
                    Action='lambda:GetLayerVersion',
                    Principal='*'
                )
                print('set permissions on layer')
            except ClientError as e:
                print('error: {}'.format(e))
                logger.error('Error creating new layer for %s:%s, error: %s' %
                    region, runtime, e.response['Error']['Message'])
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






#########################

# regions = lambda_regions()

regions = ['us-east-1', 'us-west-2']
table = db_resource.Table(table_name)

last_version_processed = latest_version()
if last_version_processed:
    # Tracking record exists
    if parse_version(version_to_process) > parse_version(last_version_processed):
        publish_layers(regions, compatible_runtimes)
else:
    # Database does not have tracking record, publish and create
    publish_layers(regions, compatible_runtimes)

# Create/update tracking record of last processed version
table.put_item(
    Item={
        'region': MASTER_REGION,
        'timestamp': MASTER_TIMESTAMP,
        'package_version': version_to_process
    }
)


# build:
#   README.md with updates for processed version and latest table of region, version, arn and other jinja entries
#   VERSION - JSON formatted file with the above details
# clone repo with token, copy over new files, commit, push



