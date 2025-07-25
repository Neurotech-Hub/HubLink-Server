import boto3
import logging
import os
from datetime import datetime, timedelta, timezone
from models import *
import json
from dateutil import parser
import time
import botocore.exceptions

def rebuild_S3_files(account_settings):
    retries = 3
    account_id = account_settings.account_id
    affected_files = []  # Track affected files

    # Validate required settings
    if not account_settings.aws_access_key_id or not account_settings.aws_secret_access_key or not account_settings.bucket_name:
        logging.error(f"Missing AWS credentials or bucket name for account {account_id}.")
        return []

    try:
        s3_client = boto3.client(
            's3',
            aws_access_key_id=account_settings.aws_access_key_id,
            aws_secret_access_key=account_settings.aws_secret_access_key,
            region_name=os.getenv('AWS_REGION', 'us-east-1')
        )
    except Exception as e:
        logging.error(f"Failed to create S3 client: {e}")
        return []

    for attempt in range(retries):
        try:
            # Get all current files in database for this account
            db_files = {file.key: file for file in File.query.filter_by(account_id=account_id).all()}
            s3_files = set()  # Track S3 files we've seen

            continuation_token = None
            while True:
                list_params = {'Bucket': account_settings.bucket_name, 'Prefix': ''}
                if continuation_token:
                    list_params['ContinuationToken'] = continuation_token

                response = s3_client.list_objects_v2(**list_params)

                # Process each file from S3
                if 'Contents' in response:
                    for obj in response['Contents']:
                        file_key = obj['Key']
                            
                        s3_files.add(file_key)

                        # rewrite all right now, check version in the future to minimize db operations
                        if file_key in db_files:
                            existing_file = db_files[file_key]
                            # Only add to affected_files if last_modified has changed
                            if existing_file.last_modified != obj['LastModified']:
                                affected_files.append(existing_file)
                            existing_file.size = obj['Size']
                            existing_file.last_modified = obj['LastModified']
                            existing_file.url = generate_s3_url(account_settings.bucket_name, file_key)
                            db.session.add(existing_file)
                        else:
                            # New file - create new entry with version 1
                            new_file = File(
                                account_id=account_id,
                                key=file_key,
                                url=generate_s3_url(account_settings.bucket_name, file_key),
                                size=obj['Size'],
                                last_modified=obj['LastModified'],
                                last_checked=datetime.now(timezone.utc),
                                version=1
                            )
                            db.session.add(new_file)
                            affected_files.append(new_file)

                # Handle pagination
                if response.get('IsTruncated'):
                    continuation_token = response.get('NextContinuationToken')
                else:
                    break

            # Remove files that exist in DB but not in S3
            for key in db_files:
                if key not in s3_files:
                    # Add to affected_files before deleting
                    affected_files.append(db_files[key])
                    db.session.delete(db_files[key])

            db.session.commit()
            logging.info(f"Successfully synchronized files for account {account_id}")

            # Only sync source files in non-production environments
            if os.getenv('ENVIRONMENT', 'development') != 'production':
                sync_source_files(account_settings)
            
            return affected_files

        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed accessing S3 bucket: {e}")
            if attempt < retries - 1:
                logging.debug("Retrying...")
            else:
                return []

def generate_download_link(account_settings, key, expires_in=3600):
    # Validate that required settings are not empty
    if not account_settings.aws_access_key_id or not account_settings.aws_secret_access_key or not account_settings.bucket_name:
        logging.error(f"Missing AWS credentials or bucket name for account {account_settings.account_id}.")
        return None

    try:
        # Create S3 client using the access key provided by the account settings
        s3_client = boto3.client(
            's3',
            aws_access_key_id=account_settings.aws_access_key_id,
            aws_secret_access_key=account_settings.aws_secret_access_key,
            region_name=os.getenv('AWS_REGION', 'us-east-1')  # Default to 'us-east-1' if not set
        )

        # Generate presigned URL for the given key (always latest version)
        params = {
            'Bucket': account_settings.bucket_name,
            'Key': key
        }

        pre_signed_url = s3_client.generate_presigned_url(
            'get_object',
            Params=params,
            ExpiresIn=expires_in  # URL valid for given time in seconds
        )
        return pre_signed_url
    except Exception as e:
        logging.error(f"Failed to generate download link for {key}: {e}")
        return None

def get_latest_files(account_id, total=1000, days=None, device_id=None):
    try:
        # Add filter to exclude files starting with '.'
        query = File.query.filter_by(account_id=account_id)\
            .filter(~File.key.like('.%'))\
            .order_by(File.last_modified.desc())
        
        # Apply a date filter if `days` is specified
        if days is not None:
            date_limit = datetime.now(datetime.UTC) - timedelta(days=days)
            query = query.filter(File.last_modified >= date_limit)

        # Apply a device_id filter if specified, filtering by key prefix
        if device_id is not None:
            device_prefix = f"{device_id}/"
            query = query.filter(File.key.like(f"{device_prefix}%"))

        # Limit the total number of files
        latest_files = query.limit(total).all()
        return latest_files

    except Exception as e:
        logging.error(f"Failed to retrieve latest files for account {account_id}: {e}")
        return []

def do_files_exist(account_id, files):
    try:
        # Extract filenames from the provided list of dictionaries
        filenames = [file['filename'] for file in files]
        
        # Query the File table for entries matching the given account_id and filenames
        existing_files = File.query.filter(File.account_id == account_id, File.key.in_(filenames)).all()
        existing_file_dict = {file.key: file.size for file in existing_files}

        # Generate a list of booleans indicating if each filename exists and has the same size
        result = [
            file['filename'] in existing_file_dict and existing_file_dict[file['filename']] == file['size']
            for file in files
        ]
        return result
    except Exception as e:
        logging.error(f"Error in 'do_files_exist' function: {e}")
        return [False] * len(files)

def generate_s3_url(bucket_name, key):
    """Generate a publicly accessible HTTPS URL for a given bucket and key"""
    return f"https://{bucket_name}.s3.amazonaws.com/{key}"

# this function is only intended to sync source files that have been generated from local testing
def sync_source_files(account_settings):
    """Sync source files with their corresponding .hublink/source/ files"""
    try:
        # Get all source files from S3 that match the pattern
        source_files = File.query.filter_by(account_id=account_settings.account_id)\
            .filter(File.key.like('.hublink/source/%.csv'))\
            .all()
        
        # Get all sources for this account
        sources = Source.query.filter_by(account_id=account_settings.account_id).all()
        
        # Create a mapping of source names to their files
        source_file_map = {
            file.key.split('/')[-1].replace('.csv', ''): file
            for file in source_files
        }
        
        # Update source.file_id for matching files
        for source in sources:
            if source.name in source_file_map:
                file = source_file_map[source.name]
                source.file_id = file.id
                source.state = 'success'
                source.last_updated = file.last_modified
            else:
                source.file_id = None
                source.state = 'error'
                
        db.session.commit()
        logging.info(f"Successfully synced source files for account {account_settings.account_id}")
        
    except Exception as e:
        logging.error(f"Error syncing source files: {e}")
        db.session.rollback()

def download_source_file(account_settings, source):
    """
    Download a source's CSV file from S3 into memory.
    Returns: CSV content as string, or None if error
    """
    if not source.file_id:
        logging.error(f"Source {source.name} ")
        return None

    try:
        # Create S3 client
        s3_client = boto3.client(
            's3',
            aws_access_key_id=account_settings.aws_access_key_id,
            aws_secret_access_key=account_settings.aws_secret_access_key,
            region_name=os.getenv('AWS_REGION', 'us-east-1')
        )

        # Get the file object from database
        file = db.session.get(File, source.file_id)
        if not file:
            logging.error(f"File {source.file_id} not found for source {source.name}")
            return None

        # Download file from S3 (always latest version)
        response = s3_client.get_object(
            Bucket=account_settings.bucket_name,
            Key=file.key
        )
        
        # Read the content as string
        csv_content = response['Body'].read().decode('utf-8')
        return csv_content

    except Exception as e:
        logging.error(f"Error downloading source file for {source.name}: {e}")
        return None

def get_source_file_header(account_settings, source, num_lines=2):
    """
    Download only the header and first data row of a source's CSV file from S3.
    
    Args:
        account_settings: Account settings containing AWS credentials
        source: Source object containing file information
        num_lines: Number of lines to read from the start (default=2 for header + first data row)
    
    Returns: 
        dict containing:
            header: String containing the header line
            first_row: String containing the first data row
            error: Error message if any
    """
    if not source.file_id:
        logging.error(f"Source {source.name} has no associated file")
        return {'header': None, 'first_row': None, 'error': 'No file associated with source'}

    try:
        # Create S3 client
        s3_client = boto3.client(
            's3',
            aws_access_key_id=account_settings.aws_access_key_id,
            aws_secret_access_key=account_settings.aws_secret_access_key,
            region_name=os.getenv('AWS_REGION', 'us-east-1')
        )

        # Get the file object from database
        file = db.session.get(File, source.file_id)
        if not file:
            logging.error(f"File {source.file_id} not found for source {source.name}")
            return {'header': None, 'first_row': None, 'error': 'File not found'}

        # Download only the first part of the file using range request
        response = s3_client.get_object(
            Bucket=account_settings.bucket_name,
            Key=file.key,
            Range='bytes=0-8192'  # Get first 8KB which should be enough for headers
        )
        
        # Read content and get first n lines
        content = response['Body'].read().decode('utf-8')
        lines = content.split('\n')[:num_lines]
        
        if len(lines) < 2:
            return {
                'header': lines[0] if lines else None,
                'first_row': None,
                'error': 'File does not contain enough lines'
            }
        
        # Add debug logging
        logging.info(f"Header line: {lines[0]}")
        logging.info(f"First data row: {lines[1]}")
            
        return {
            'header': lines[0].strip(),
            'first_row': lines[1].strip(),
            'error': None
        }

    except Exception as e:
        logging.error(f"Error downloading header for source {source.name}: {e}")
        return {'header': None, 'first_row': None, 'error': str(e)}

def setup_aws_resources(admin_settings, new_bucket_name, new_user_name, version_files=True, version_days=7):
    """
    Creates AWS resources (S3 bucket and IAM user) for a new account.
    Uses admin credentials to create these resources.
    
    Args:
        admin_settings: Setting object containing admin AWS credentials
        new_bucket_name: Name of the S3 bucket to create
        new_user_name: Name of the IAM user to create
        version_files: Whether to enable versioning (default: True) [CURRENTLY DISABLED]
        version_days: Number of days to keep old versions (default: 7) [CURRENTLY DISABLED]
    """
    try:
        logging.info(f"Setting up AWS resources with admin credentials")
        logging.info(f"Creating bucket: {new_bucket_name}")
        logging.info(f"Creating IAM user: {new_user_name}")
        logging.info(f"Using region: {os.getenv('AWS_REGION', 'us-east-1')}")
        
        # Test AWS credentials before proceeding
        try:
            sts_client = boto3.client(
                'sts',
                aws_access_key_id=admin_settings.aws_access_key_id,
                aws_secret_access_key=admin_settings.aws_secret_access_key,
                region_name=os.getenv('AWS_REGION', 'us-east-1')
            )
            identity = sts_client.get_caller_identity()
            logging.info(f"AWS credentials valid. Account ID: {identity['Account']}")
        except Exception as e:
            logging.error(f"AWS credentials test failed: {str(e)}")
            return False, None, f"Invalid AWS credentials: {str(e)}"

        # Create boto3 clients using admin credentials
        s3_client = boto3.client(
            's3',
            aws_access_key_id=admin_settings.aws_access_key_id,
            aws_secret_access_key=admin_settings.aws_secret_access_key,
            region_name=os.getenv('AWS_REGION', 'us-east-1')
        )
        
        iam_client = boto3.client(
            'iam',
            aws_access_key_id=admin_settings.aws_access_key_id,
            aws_secret_access_key=admin_settings.aws_secret_access_key,
            region_name=os.getenv('AWS_REGION', 'us-east-1')
        )

        # Pre-check if bucket exists
        try:
            s3_client.head_bucket(Bucket=new_bucket_name)
            logging.error(f"Bucket {new_bucket_name} already exists")
            return False, None, f"Bucket {new_bucket_name} already exists"
        except s3_client.exceptions.ClientError as e:
            error_code = e.response.get('Error', {}).get('Code')
            if error_code != '404':  # If error is not 'bucket does not exist'
                logging.error(f"Error checking bucket existence: {str(e)}")
                return False, None, f"Error checking bucket: {str(e)}"

        # Pre-check if IAM user exists
        try:
            iam_client.get_user(UserName=new_user_name)
            logging.error(f"IAM user {new_user_name} already exists")
            return False, None, f"IAM user {new_user_name} already exists"
        except iam_client.exceptions.NoSuchEntityException:
            pass  # This is what we want - user doesn't exist
        except Exception as e:
            logging.error(f"Error checking IAM user existence: {str(e)}")
            return False, None, f"Error checking IAM user: {str(e)}"

        # 1. Create S3 bucket
        try:
            if os.getenv('AWS_REGION', 'us-east-1') == 'us-east-1':
                # For us-east-1, don't specify LocationConstraint
                s3_client.create_bucket(
                    Bucket=new_bucket_name,
                    ObjectOwnership='BucketOwnerPreferred'
                )
            else:
                # For other regions, specify LocationConstraint
                s3_client.create_bucket(
                    Bucket=new_bucket_name,
                    ObjectOwnership='BucketOwnerPreferred',
                    CreateBucketConfiguration={
                        'LocationConstraint': os.getenv('AWS_REGION', 'us-east-1')
                    }
                )
            logging.info(f"Successfully created bucket: {new_bucket_name}")

            """
            IMPORTANT NOTE FOR AI AGENTS:
            The following versioning configuration is temporarily disabled.
            Do not remove these commented sections as they may be re-enabled
            when bucket versioning becomes a supported feature again.
            """
            # # Enable versioning on the bucket
            # s3_client.put_bucket_versioning(
            #     Bucket=new_bucket_name,
            #     VersioningConfiguration={
            #         'Status': 'Enabled' if version_files else 'Suspended'
            #     }
            # )
            # logging.info(f"{'Enabled' if version_files else 'Disabled'} versioning for bucket: {new_bucket_name}")

            # # Set lifecycle configuration if versioning is enabled
            # if version_files:
            #     lifecycle_config = {
            #         'Rules': [
            #             {
            #                 'ID': 'DeleteOldVersions',
            #                 'Status': 'Enabled',
            #                 'NoncurrentVersionExpiration': {
            #                     'NoncurrentDays': version_days
            #                 },
            #                 'ExpiredObjectDeleteMarker': True,
            #                 'AbortIncompleteMultipartUpload': {
            #                     'DaysAfterInitiation': 7
            #                 }
            #             }
            #         ]
            #     }
            #     s3_client.put_bucket_lifecycle_configuration(
            #         Bucket=new_bucket_name,
            #         LifecycleConfiguration=lifecycle_config
            #     )
            #     logging.info(f"Set lifecycle configuration to delete old versions after {version_days} days, with cleanup of delete markers and incomplete uploads")

            # Disable block public access settings for the bucket
            s3_client.put_public_access_block(
                Bucket=new_bucket_name,
                PublicAccessBlockConfiguration={
                    'BlockPublicAcls': False,
                    'IgnorePublicAcls': False,
                    'BlockPublicPolicy': False,
                    'RestrictPublicBuckets': False
                }
            )
            logging.info(f"Disabled block public access settings for bucket: {new_bucket_name}")

            # Wait a moment for the settings to propagate
            time.sleep(2)

        except s3_client.exceptions.BucketAlreadyExists:
            logging.error(f"Bucket {new_bucket_name} already exists")
            return False, None, f"Bucket {new_bucket_name} already exists"
        except Exception as e:
            logging.error(f"Failed to create bucket: {str(e)}")
            return False, None, f"Failed to create bucket: {str(e)}"

        # 2. Apply bucket policy to make sources public
        bucket_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicReadGetObject",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": f"arn:aws:s3:::{new_bucket_name}/.hublink/source/*"
                }
            ]
        }
        s3_client.put_bucket_policy(
            Bucket=new_bucket_name,
            Policy=json.dumps(bucket_policy)
        )

        # 3. Create IAM user
        try:
            iam_client.create_user(UserName=new_user_name)
            logging.info(f"Successfully created IAM user: {new_user_name}")
        except iam_client.exceptions.EntityAlreadyExistsException:
            # Cleanup bucket before returning error
            logging.error(f"IAM user {new_user_name} already exists")
            try:
                s3_client.delete_bucket(Bucket=new_bucket_name)
                logging.info(f"Cleaned up bucket {new_bucket_name} after user creation failed")
            except Exception as cleanup_error:
                logging.error(f"Failed to cleanup bucket: {cleanup_error}")
            return False, None, f"IAM user {new_user_name} already exists"
        except Exception as e:
            logging.error(f"Failed to create IAM user: {str(e)}")
            # Cleanup bucket since user creation failed
            try:
                s3_client.delete_bucket(Bucket=new_bucket_name)
                logging.info(f"Cleaned up bucket {new_bucket_name} after user creation failed")
            except Exception as cleanup_error:
                logging.error(f"Failed to cleanup bucket: {cleanup_error}")
            return False, None, f"Failed to create IAM user: {str(e)}"

        # 4. Create and attach user policy; keep all permissions for now even though versioning is disabled
        try:
            user_policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "s3:ListBucket",
                            "s3:ListBucketVersions",
                            "s3:PutLifecycleConfiguration",
                            "s3:GetLifecycleConfiguration"
                        ],
                        "Resource": f"arn:aws:s3:::{new_bucket_name}"
                    },
                    {
                        "Effect": "Allow",
                        "Action": [
                            "s3:GetObject",
                            "s3:PutObject",
                            "s3:DeleteObject",
                            "s3:DeleteObjectVersion",
                            "s3:DeleteObjectVersionTagging",
                            "s3:GetObjectVersion"
                        ],
                        "Resource": f"arn:aws:s3:::{new_bucket_name}/*"
                    }
                ]
            }

            policy_name = f"{new_user_name}-s3-access"
            policy_response = iam_client.create_policy(
                PolicyName=policy_name,
                PolicyDocument=json.dumps(user_policy)
            )
            logging.info(f"Created IAM policy: {policy_name}")

            iam_client.attach_user_policy(
                UserName=new_user_name,
                PolicyArn=policy_response['Policy']['Arn']
            )
            logging.info(f"Attached policy to user {new_user_name}")
        except Exception as e:
            logging.error(f"Failed to create/attach policy: {str(e)}")
            # Cleanup user and bucket
            try:
                iam_client.delete_user(UserName=new_user_name)
                s3_client.delete_bucket(Bucket=new_bucket_name)
                logging.info(f"Cleaned up user and bucket after policy creation failed")
            except Exception as cleanup_error:
                logging.error(f"Failed to cleanup resources: {cleanup_error}")
            return False, None, f"Failed to setup user permissions: {str(e)}"

        # 5. Create access key for the user
        try:
            key_response = iam_client.create_access_key(UserName=new_user_name)
            logging.info(f"Created access key for user {new_user_name}")
        except Exception as e:
            logging.error(f"Failed to create access key: {str(e)}")
            # Cleanup everything
            try:
                iam_client.detach_user_policy(
                    UserName=new_user_name,
                    PolicyArn=policy_response['Policy']['Arn']
                )
                iam_client.delete_policy(PolicyArn=policy_response['Policy']['Arn'])
                iam_client.delete_user(UserName=new_user_name)
                s3_client.delete_bucket(Bucket=new_bucket_name)
                logging.info(f"Cleaned up all resources after access key creation failed")
            except Exception as cleanup_error:
                logging.error(f"Failed to cleanup resources: {cleanup_error}")
            return False, None, f"Failed to create access key: {str(e)}"

        credentials = {
            'aws_access_key_id': key_response['AccessKey']['AccessKeyId'],
            'aws_secret_access_key': key_response['AccessKey']['SecretAccessKey'],
            'bucket_name': new_bucket_name
        }

        return True, credentials, None

    except Exception as e:
        logging.error(f"Unexpected error in setup_aws_resources: {str(e)}")
        
        # Only attempt cleanup if we got past bucket creation
        if 'new_bucket_name' in locals():
            # Attempt to cleanup any resources that were created
            try:
                s3_client.delete_bucket(Bucket=new_bucket_name)
                logging.info(f"Cleaned up bucket {new_bucket_name} after unexpected error")
            except Exception as cleanup_error:
                logging.error(f"Error during cleanup: {cleanup_error}")

        return False, None, f"Error setting up AWS resources: {str(e)}"

def cleanup_aws_resources(admin_settings, user_name, bucket_name):
    """
    Cleanup AWS resources when account creation fails.
    
    Args:
        admin_settings: Setting object containing admin AWS credentials
        user_name: Name of the IAM user to delete
        bucket_name: Name of the S3 bucket to delete
    """
    try:
        # Create boto3 clients using admin credentials
        s3_client = boto3.client(
            's3',
            aws_access_key_id=admin_settings.aws_access_key_id,
            aws_secret_access_key=admin_settings.aws_secret_access_key,
            region_name=os.getenv('AWS_REGION', 'us-east-1')
        )
        
        iam_client = boto3.client(
            'iam',
            aws_access_key_id=admin_settings.aws_access_key_id,
            aws_secret_access_key=admin_settings.aws_secret_access_key,
            region_name=os.getenv('AWS_REGION', 'us-east-1')
        )

        # 1. List and delete all access keys for the user
        try:
            keys = iam_client.list_access_keys(UserName=user_name)
            for key in keys.get('AccessKeyMetadata', []):
                iam_client.delete_access_key(
                    UserName=user_name,
                    AccessKeyId=key['AccessKeyId']
                )
        except Exception as e:
            logging.error(f"Error deleting access keys: {e}")

        # 2. List and detach all user policies
        try:
            policies = iam_client.list_attached_user_policies(UserName=user_name)
            for policy in policies.get('AttachedPolicies', []):
                iam_client.detach_user_policy(
                    UserName=user_name,
                    PolicyArn=policy['PolicyArn']
                )
                # Also delete the policy if it's a user-specific policy
                if user_name in policy['PolicyArn']:
                    iam_client.delete_policy(PolicyArn=policy['PolicyArn'])
        except Exception as e:
            logging.error(f"Error cleaning up policies: {e}")

        # 3. Delete the IAM user
        try:
            iam_client.delete_user(UserName=user_name)
        except Exception as e:
            logging.error(f"Error deleting IAM user: {e}")

        # 4. Delete the S3 bucket
        try:
            # First, delete all objects in the bucket
            paginator = s3_client.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=bucket_name):
                if 'Contents' in page:
                    objects = [{'Key': obj['Key']} for obj in page['Contents']]
                    s3_client.delete_objects(
                        Bucket=bucket_name,
                        Delete={'Objects': objects}
                    )
            
            # Then delete the bucket itself
            s3_client.delete_bucket(Bucket=bucket_name)
        except Exception as e:
            logging.error(f"Error deleting S3 bucket: {e}")

    except Exception as e:
        logging.error(f"Error during AWS resource cleanup: {e}")
        raise

def download_s3_file(account_settings, file):
    """
    Download a file from S3 into memory.
    Returns: File content as bytes, or None if error
    
    Args:
        account_settings: Setting object containing AWS credentials
        file: File object containing the key to download
    """
    try:
        # Test S3 client creation independently
        s3_client = boto3.client(
            's3',
            aws_access_key_id=account_settings.aws_access_key_id,
            aws_secret_access_key=account_settings.aws_secret_access_key,
            region_name=os.getenv('AWS_REGION', 'us-east-1')
        )

        # Test bucket access
        s3_client.head_bucket(Bucket=account_settings.bucket_name)

        # Download file from S3 (always latest version)
        response = s3_client.get_object(
            Bucket=account_settings.bucket_name,
            Key=file.key
        )

        # Read the content as bytes
        content = response['Body'].read()
        
        return content

    except botocore.exceptions.ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        error_message = e.response.get('Error', {}).get('Message', str(e))
        print(f"DEBUG: AWS Error - Code: {error_code}, Message: {error_message}")
        logging.error(f"AWS error downloading file {file.key}: {error_message}")
        return None
    except Exception as e:
        print(f"DEBUG: General error: {str(e)}")
        logging.error(f"Error downloading file {file.key}: {str(e)}")
        return None

def delete_files_from_s3(account_settings, files):
    """
    Delete multiple files from S3 and the database.
    
    Args:
        account_settings: Setting object containing AWS credentials
        files: List of File objects to delete
        
    Returns:
        Tuple of (success, error_message)
    """
    try:
        logging.info(f"Starting deletion of {len(files)} files from S3")
        
        # Create S3 client
        s3_client = boto3.client(
            's3',
            aws_access_key_id=account_settings.aws_access_key_id,
            aws_secret_access_key=account_settings.aws_secret_access_key,
            region_name=os.getenv('AWS_REGION', 'us-east-1')
        )
        
        # Track processed files to prevent duplicates
        processed_files = set()
        
        # Delete each file from S3
        for file in files:
            if file.key in processed_files:
                logging.warning(f"Skipping duplicate file: {file.key}")
                continue
                
            try:
                logging.info(f"Starting deletion of file: {file.key}")
                
                # Delete all versions of the file
                versions = s3_client.list_object_versions(
                    Bucket=account_settings.bucket_name,
                    Prefix=file.key
                )
                
                deletion_errors = []
                
                # Delete version markers first
                if 'DeleteMarkers' in versions:
                    delete_markers = [
                        {'Key': file.key, 'VersionId': marker['VersionId']}
                        for marker in versions['DeleteMarkers']
                    ]
                    if delete_markers:
                        response = s3_client.delete_objects(
                            Bucket=account_settings.bucket_name,
                            Delete={'Objects': delete_markers}
                        )
                        if 'Errors' in response:
                            deletion_errors.extend(response['Errors'])
                            logging.error(f"Errors deleting markers for {file.key}: {response['Errors']}")
                
                # Then delete all versions
                if 'Versions' in versions:
                    version_objects = [
                        {'Key': file.key, 'VersionId': version['VersionId']}
                        for version in versions['Versions']
                    ]
                    if version_objects:
                        response = s3_client.delete_objects(
                            Bucket=account_settings.bucket_name,
                            Delete={'Objects': version_objects}
                        )
                        if 'Errors' in response:
                            deletion_errors.extend(response['Errors'])
                            logging.error(f"Errors deleting versions for {file.key}: {response['Errors']}")
                
                # Verify file is completely deleted
                try:
                    s3_client.head_object(
                        Bucket=account_settings.bucket_name,
                        Key=file.key
                    )
                    logging.error(f"File {file.key} still exists after deletion attempt")
                    continue
                except s3_client.exceptions.ClientError as e:
                    error_code = e.response.get('Error', {}).get('Code')
                    if error_code == '404':
                        # File is confirmed deleted
                        if not deletion_errors:
                            db.session.delete(file)
                            processed_files.add(file.key)
                            logging.info(f"Successfully deleted file: {file.key}")
                        else:
                            logging.error(f"Skipping database deletion for {file.key} due to S3 errors")
                    else:
                        logging.error(f"Error verifying deletion of {file.key}: {error_code}")
                        continue
                
            except botocore.exceptions.ClientError as e:
                error_code = e.response.get('Error', {}).get('Code', 'Unknown')
                error_message = e.response.get('Error', {}).get('Message', str(e))
                logging.error(f"AWS Error deleting file {file.key}: {error_code} - {error_message}")
                continue
            except Exception as e:
                logging.error(f"Error deleting file {file.key}: {str(e)}")
                continue
        
        if processed_files:
            db.session.commit()
            logging.info(f"Successfully completed deletion of {len(processed_files)} files")
            return True, None
        else:
            logging.error("No files were successfully processed")
            return False, "No files were successfully deleted"
        
    except Exception as e:
        logging.error(f"Error in delete_files_from_s3: {str(e)}")
        db.session.rollback()
        return False, str(e)

def get_storage_usage(account_settings):
    """
    Get both current total size and versioned total size of an S3 bucket.
    
    Args:
        account_settings: Setting object containing AWS credentials and bucket name
        
    Returns:
        Dictionary containing:
            current_size: Total size of current (latest) files
            versioned_size: Total size of non-current versions only
        Returns None if error occurs
    """
    try:
        s3_client = boto3.client(
            's3',
            aws_access_key_id=account_settings.aws_access_key_id,
            aws_secret_access_key=account_settings.aws_secret_access_key,
            region_name=os.getenv('AWS_REGION', 'us-east-1')
        )
        
        current_size = 0
        versioned_size = 0
        
        # Get all current objects first
        paginator = s3_client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=account_settings.bucket_name):
            if 'Contents' in page:
                for obj in page['Contents']:
                    current_size += obj['Size']
        
        # Get all versions of all objects
        paginator = s3_client.get_paginator('list_object_versions')
        for page in paginator.paginate(Bucket=account_settings.bucket_name):
            if 'Versions' in page:
                for version in page['Versions']:
                    # Only count non-current versions for versioned_size
                    if not version.get('IsLatest', False):
                        versioned_size += version['Size']
        
        return {
            'current_size': current_size,
            'versioned_size': versioned_size  # Now only includes non-current versions
        }
        
    except Exception as e:
        logging.error(f"Error calculating bucket sizes: {e}")
        return None

def update_specific_files(account_settings, file_keys):
    """
    Update or create database entries for specific S3 files.
    
    Args:
        account_settings: Setting object containing AWS credentials
        file_keys: List of file keys to check/update
    
    Returns:
        List of affected File objects
    """
    affected_files = []
    
    try:
        s3_client = boto3.client(
            's3',
            aws_access_key_id=account_settings.aws_access_key_id,
            aws_secret_access_key=account_settings.aws_secret_access_key,
            region_name=os.getenv('AWS_REGION', 'us-east-1')
        )
        
        # Get existing files from database
        existing_files = {
            file.key: file 
            for file in File.query.filter_by(
                account_id=account_settings.account_id
            ).filter(File.key.in_(file_keys)).all()
        }
        
        for file_key in file_keys:
            try:
                # Get object metadata from S3
                obj = s3_client.head_object(
                    Bucket=account_settings.bucket_name,
                    Key=file_key
                )
                
                if file_key in existing_files:
                    # Update existing file
                    existing_file = existing_files[file_key]
                    # If size or last_modified has changed, increment version
                    if (existing_file.size != obj['ContentLength'] or 
                        existing_file.last_modified != obj['LastModified']):
                        existing_file.version += 1
                        affected_files.append(existing_file)
                    
                    existing_file.size = obj['ContentLength']
                    existing_file.last_modified = obj['LastModified']
                    existing_file.url = generate_s3_url(account_settings.bucket_name, file_key)
                    db.session.add(existing_file)
                else:
                    # Create new file entry starting at version 1
                    new_file = File(
                        account_id=account_settings.account_id,
                        key=file_key,
                        url=generate_s3_url(account_settings.bucket_name, file_key),
                        size=obj['ContentLength'],
                        last_modified=obj['LastModified'],
                        last_checked=datetime.now(timezone.utc),
                        version=1
                    )
                    db.session.add(new_file)
                    affected_files.append(new_file)
                
            except botocore.exceptions.ClientError as e:
                error_code = e.response.get('Error', {}).get('Code')
                if error_code == '404':
                    # File doesn't exist in S3, remove from DB if present
                    if file_key in existing_files:
                        db.session.delete(existing_files[file_key])
                else:
                    logging.error(f"Error checking file {file_key}: {str(e)}")
                    continue
                    
        db.session.commit()
        return affected_files
        
    except Exception as e:
        logging.error(f"Error in update_specific_files: {str(e)}")
        db.session.rollback()
        return []