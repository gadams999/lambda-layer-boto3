# lambda-layer-boto3

Use one of these boto3 layers when you want:

```python
>>> import boto3
>>> boto3.__version__
'{{new_version}}'
```

instead of:

```python
>>> import boto3
>>> boto3.__version__
'1.7.74'
```

## Overview

This repository monitors for new versions of the python boto3 package to be published on pypi. It then builds new [Lambda layers](https://docs.aws.amazon.com/lambda/latest/dg/configuration-layers.html)  and publishes them to all support AWS regions. They can be referenced from your functions to provide the latest boto3 and botocore functionality for python 2.7, 3.6, and 3.7.

For each specific python version (and one that combines all versions), you can reference the ARN when creating or modifying a new python Lambda function via the console, AWS CLI, CloudFormation template, or programmatically using an SDK.