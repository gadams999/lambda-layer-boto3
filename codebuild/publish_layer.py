#!/usr/bin/env python3

import requests
import sys
import os
import logging
from pkg_resources import parse_version
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

version_to_process = os.environ['PKG_VERSION']

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



regions = lambda_regions()

# open ddb table and read latest processed/pub version (pk "region": "default")
# compare to package version on pypi

# >>> from pkg_resources import parse_version
# >>> parse_version('1.9.a.dev') == parse_version('1.9a0dev')
# True
# >>> parse_version('2.1-rc2') < parse_version('2.1')
# True
# >>> parse_version('0.6a9dev-r41475') < parse_version('0.6a9')
# True

# if pypi version newer than ddb version

# for every lambda region:
#    compatible_runtimes = {python2.7: python2.7, python3.6, python3.7, combined: [python2.7, python3.6, python3.7]}
#    for compatible_runtimes:
#      try:
#         publish layer
#         create DDB entry (pk: "region": "$region",
#                           timestamp: time.time(),
#                           "arn": returned arn,
#                           "version": "boto3 version",
#                           "runtimes": [python3.6, ...])
#         procesed_entry = success, region, arn, runtimes
#      except ClientError:
#         processed_entry = fail, region, runtimes

# build:
#   README.md with updates for processed version and latest table of region, version, arn and other jinja entries
#   VERSION - JSON formatted file with the above details
# clone repo with token, copy over new files, commit, push



