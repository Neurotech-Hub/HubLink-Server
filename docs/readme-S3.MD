# S3 and IAM Configuration for Secure File Access

This guide outlines the steps to configure an Amazon S3 bucket and IAM user for secure file uploads, downloads, and listing operations in your web application.

## 1. Create an S3 Bucket

1. Log in to the [AWS Management Console](https://aws.amazon.com/console/)
2. Navigate to the **S3 Dashboard**
3. Click **Create bucket** and configure:
   - **Bucket Name**: Choose a unique name (e.g., `my-app-files`)
   - **Region**: Select a region closest to your application
   - **Permissions**:  
     - Only check the two boxes for ACLs
     - Leave other settings as default
4. Click **Create bucket**
5. Modify the bucket policy to allow public access to files:
``` json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "PublicReadGetObject",
            "Effect": "Allow",
            "Principal": "*",
            "Action": "s3:GetObject",
            "Resource": "arn:aws:s3:::neurotechhub-000/.hublink/source/*"
        }
    ]
}

## 2. Set Up IAM User for Bucket Access

1. Navigate to the [IAM Dashboard](https://console.aws.amazon.com/iam/)
2. Click **Users** → **Add users**:
   - **User Name**: Use a descriptive name (e.g., `neurotechhub`)
   - **Policies**: If prompted, you may select to copy policies from an existing user and modify them.
   - **Click Create Access Key** (select *Application running outside AWS*)
   - **Set Tag Description**: `primary`
   - Download the Access Key .csv file and archive it
3. Click **Next: Permissions**

### Assign Permissions

1. Create a Policy using the JSON Editor:
    ```json
    {
      "Version": "2012-10-17",
      "Statement": [
        {
          "Effect": "Allow",
          "Action": [
            "s3:ListBucket"
          ],
          "Resource": "arn:aws:s3:::neurotechhub-000"
        },
        {
          "Effect": "Allow",
          "Action": [
            "s3:GetObject",
            "s3:PutObject",
            "s3:DeleteObject"
          ],
          "Resource": "arn:aws:s3:::neurotechhub-000/*"
        },
      ]
    }
    ```
2. Name the policy: `user-access-s3`
3. Run the `test_s3.py` script to verify the user has access to the bucket

## Error Handling

The `test_s3.py` script includes error handling for:
- Missing or invalid credential files
- CSV parsing errors
- Invalid bucket names
- File operations failures
- S3 upload failures
- AWS configuration issues
- Resource cleanup errors

If any errors occur, the script will display a detailed error message and exit with status code 1.

## 3. Create a Lambda Function and Policy/Role

1. Navigate to the [Lambda Dashboard](https://console.aws.amazon.com/lambda/home)
2. Click **Create function**
3. Configure the function with the following settings:
    - **Function Name**: `hublink-source`
    - **Runtime**: `Python 3.11`
    - **Role**: `Create new role with basic Lambda permissions`
    - **Existing role**: `lambda-basic-execution`
4. Enable **Function URL** and allow anonymous access.

### Create a Policy for the Lambda Function (do once)

1. Goto IAM Dashboard
2. Click **Policies**
3. Click **Create policy**
4. Click **JSON**
5. Paste the policy:

    ```json
  {
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": [
          "s3:ListBucket",
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject"
        ],
        "Resource": "*"
      },
      {
        "Effect": "Allow",
        "Action": "lambda:InvokeFunction",
        "Resource": "arn:aws:lambda:us-east-1:557690613785:function:hublink-source"
      }
    ]
  }
    ```

6. Name the policy `LambdaS3AccessPolicy`
7. Click **Create policy**
8. Attach the policy to the role created in step 3.
  - Click **Roles**
  - Permissions > Add permissions > Attach policies
  - Search for `LambdaS3AccessPolicy` and select it
  