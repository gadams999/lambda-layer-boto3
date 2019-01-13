# Cloud Build Steps

## Prerequisites

1. CodeBuild must have a connection to GitHub with an access token (not OAuth) via the Console prior to running the script. Create new project, in *Source->Source provider* change to GitHub, select *Connect with a GitHub personal access token*, enter the value then click *Save token*. Then cancel out of *Create build project*.

1. To protect the access token, create an entry in the *AWS Systems Manager Parameter Store* in the same region where the CloudFormation template will be run:

   **Name**: `GITHUB_KEY` (or any other name, used as the GitHubKey parameter value in CloudFormation)

   **Type**: String

   **Value**: paste the access key value

   :exclamation: NOTE: Anyone with access to AWS Systems Manager Parameter Store will be able to see the key.