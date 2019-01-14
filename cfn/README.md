# Cloud Build Steps

## Prerequisites

1. CodeBuild must have a connection to GitHub with an access token (not OAuth) via the Console prior to running the script. Create new project, in *Source->Source provider* change to GitHub, select *Connect with a GitHub personal access token*, enter the value then click *Save token*. Then cancel out of *Create build project*.

1. To protect the access token, create an entry in the *AWS Systems Manager Parameter Store* in the same region where the CloudFormation template will be run:

   **Name**: `/CodeBuild/GITHUB_KEY` (or any other name prefixed by `/CodeBuild`, used as the GitHubKey parameter value in CloudFormation)

   **Type**: SecureString

   **Value**: paste the access key value

