import boto3
import logging
import os
from datetime import datetime, timedelta
from models import *
import json
from dateutil import parser
import time

def rebuild_S3_files(account_settings):
    retries = 3
    account_id = account_settings.account_id
    new_files_count = 0  # Track number of new files

    # Validate required settings
    if not account_settings.aws_access_key_id or not account_settings.aws_secret_access_key or not account_settings.bucket_name:
        logging.error(f"Missing AWS credentials or bucket name for account {account_id}.")
        return 0

    try:
        s3_client = boto3.client(
            's3',
            aws_access_key_id=account_settings.aws_access_key_id,
            aws_secret_access_key=account_settings.aws_secret_access_key,
            region_name=os.getenv('AWS_REGION', 'us-east-1')
        )
    except Exception as e:
        logging.error(f"Failed to create S3 client: {e}")
        return 0

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

                        if file_key in db_files:
                            # File exists - check if size has changed
                            existing_file = db_files[file_key]
                            if existing_file.size != obj['Size']:
                                logging.info(f"File {file_key} size changed from {existing_file.size} to {obj['Size']}")
                                existing_file.size = obj['Size']
                                existing_file.last_modified = obj['LastModified']
                                existing_file.last_checked = datetime.now(timezone.utc)
                                existing_file.version += 1  # Increment version when size changes
                            # always update the url and commit
                            existing_file.url = generate_s3_url(account_settings.bucket_name, file_key)
                            db.session.add(existing_file)
                        else:
                            # New file - create new entry
                            new_file = File(
                                account_id=account_id,
                                key=file_key,
                                url=generate_s3_url(account_settings.bucket_name, file_key),
                                size=obj['Size'],
                                last_modified=obj['LastModified'],
                                last_checked=datetime.now(timezone.utc),
                                version=1  # Initial version for new files
                            )
                            db.session.add(new_file)
                            new_files_count += 1

                # Handle pagination
                if response.get('IsTruncated'):
                    continuation_token = response.get('NextContinuationToken')
                else:
                    break

            # Remove files that exist in DB but not in S3
            for key in db_files:
                if key not in s3_files:
                    db.session.delete(db_files[key])

            db.session.commit()
            logging.info(f"Successfully synchronized files for account {account_id}")

            # After successful rebuild, sync source files
            sync_source_files(account_settings)
            
            return new_files_count

        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed accessing S3 bucket: {e}")
            if attempt < retries - 1:
                logging.debug("Retrying...")
            else:
                return 0

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
        # Generate presigned URL for the given key
        pre_signed_url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': account_settings.bucket_name, 'Key': key},
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

def get_unique_devices(account_id):
    # Query to get distinct device IDs based on the prefix structure in `File.key`
    device_ids = (
        File.query
        .filter_by(account_id=account_id)
        .filter(~File.key.like('.%'))  # Exclude keys starting with '.'
        .with_entities(File.key)
        .distinct()
    )

    # Extract and return only the part of the key before the first '/'
    unique_devices = {key.key.split('/')[0] for key in device_ids}
    # Remove any remaining hidden entries (just in case)
    unique_devices = {device for device in unique_devices if not device.startswith('.')}
    return sorted(unique_devices)

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

# def process_sqs_messages(account_settings):
#     sqs_client = boto3.client(
#         'sqs',
#         aws_access_key_id=account_settings.aws_access_key_id,
#         aws_secret_access_key=account_settings.aws_secret_access_key,
#         region_name=os.getenv('AWS_REGION', 'us-east-1')
#     )
    
#     queue_url = os.getenv('HUBLINK_QUEUE') # "https://sqs.us-east-1.amazonaws.com/557690613785/HublinkQueue"
    
#     while True:
#         try:
#             logging.debug("Polling SQS queue for messages...")
#             response = sqs_client.receive_message(
#                 QueueUrl=queue_url,
#                 MaxNumberOfMessages=10,
#                 WaitTimeSeconds=10  # Long polling
#             )
            
#             # Check if there are any messages
#             if 'Messages' in response:
#                 logging.info(f"Received {len(response['Messages'])} messages from SQS.")

#                 for message in response['Messages']:
#                     # Parse the message body to get the S3 event
#                     message_body = json.loads(message['Body'])
                    
#                     # Check if "Message" is in the message body, as it sometimes contains nested JSON
#                     if 'Message' in message_body:
#                         s3_event = json.loads(message_body['Message'])
#                     else:
#                         s3_event = message_body
                        
#                     # Process each S3 event
#                     for record in s3_event.get('Records', []):
#                         bucket_name = record['s3']['bucket']['name']
#                         file_key = record['s3']['object']['key']
#                         file_size = record['s3']['object'].get('size', 0)  # Default to 0 for deletion events
#                         last_modified_str = record.get('eventTime')

#                         # Convert last_modified from string to datetime
#                         last_modified = parser.parse(last_modified_str) if last_modified_str else None

#                         logging.info(f"Processing S3 record for bucket '{bucket_name}', key '{file_key}'.")

#                         # Only process messages for the configured bucket
#                         if bucket_name == account_settings.bucket_name:
#                             # Check if file already exists
#                             existing_file = File.query.filter_by(account_id=account_settings.account_id, key=file_key).first()
                            
#                             if existing_file:
#                                 logging.debug(f"Updating existing file: {file_key}")
#                                 # Update existing file details if necessary
#                                 existing_file.size = file_size
#                                 existing_file.last_modified = last_modified
#                                 existing_file.version += 1  # Increment version when updating last_modified
#                                 db.session.commit()
#                             else:
#                                 logging.debug(f"Inserting new file record: {file_key}")
#                                 # Add new file entry to the database
#                                 new_file = File(
#                                     account_id=account_settings.account_id,
#                                     key=file_key,
#                                     url=f"s3://{bucket_name}/{file_key}",
#                                     size=file_size,
#                                     last_modified=last_modified,
#                                     last_checked=None,
#                                     version=1  # Set initial version for new files
#                                 )
#                                 db.session.add(new_file)
#                                 db.session.commit()

#                     # Delete the message after processing
#                     sqs_client.delete_message(
#                         QueueUrl=queue_url,
#                         ReceiptHandle=message['ReceiptHandle']
#                     )
#                     logging.debug(f"Deleted message from SQS: {message['MessageId']}")
#             else:
#                 logging.info("No new messages in the queue.")
#                 break  # Exit the loop if no messages are found
            
#         except Exception as e:
#             logging.error(f"Error processing SQS messages: {e}")
#             break

def delete_device_files_from_s3(account_settings, device_id):
    """
    Delete all files for a specific device from S3.
    Returns (success, error_message)
    """
    # Validate required settings
    if not account_settings.aws_access_key_id or not account_settings.aws_secret_access_key or not account_settings.bucket_name:
        return False, "Missing AWS credentials or bucket name"

    try:
        # Create S3 client
        s3_client = boto3.client(
            's3',
            aws_access_key_id=account_settings.aws_access_key_id,
            aws_secret_access_key=account_settings.aws_secret_access_key,
            region_name=os.getenv('AWS_REGION', 'us-east-1')
        )

        # Get all files for this device from the database to know what to delete
        files_to_delete = File.query.filter_by(
            account_id=account_settings.account_id
        ).filter(
            File.key.startswith(f"{device_id}/")
        ).all()

        # Delete files from S3
        failed_deletions = []
        for file in files_to_delete:
            try:
                s3_client.delete_object(
                    Bucket=account_settings.bucket_name,
                    Key=file.key
                )
            except Exception as e:
                failed_deletions.append(f"{file.key}: {str(e)}")
                logging.error(f"Failed to delete file {file.key} from S3: {e}")

        # Return results
        if failed_deletions:
            return False, f"Failed to delete some files: {', '.join(failed_deletions)}"
        return True, None

    except Exception as e:
        error_msg = f"Error deleting files from S3: {str(e)}"
        logging.error(error_msg)
        return False, error_msg

def generate_s3_url(bucket_name, key):
    """Generate a publicly accessible HTTPS URL for a given bucket and key"""
    return f"https://{bucket_name}.s3.amazonaws.com/{key}"

# this function is only intended to sync source files that have been generated from local testing
def sync_source_files(account_settings):
    """Sync source files with their corresponding .hublink/source/ files"""
    try:
        print(f"Starting source file sync for account {account_settings.account_id}")
        
        # Get all source files from S3 that match the pattern
        source_files = File.query.filter_by(account_id=account_settings.account_id)\
            .filter(File.key.like('.hublink/source/%.csv'))\
            .all()
        print(f"Found {len(source_files)} source files in S3")
        
        # Get all sources for this account
        sources = Source.query.filter_by(account_id=account_settings.account_id).all()
        print(f"Found {len(sources)} sources in database")
        
        # Create a mapping of source names to their files
        source_file_map = {
            file.key.split('/')[-1].replace('.csv', ''): file
            for file in source_files
        }
        print(f"Source file mapping: {list(source_file_map.keys())}")
        
        # Update source.file_id for matching files
        for source in sources:
            if source.name in source_file_map:
                file = source_file_map[source.name]
                source.file_id = file.id
                source.state = 'success'
                source.last_updated = file.last_modified
                print(f"Updated source {source.name} with file {file.key}, last_modified: {file.last_modified}")
            else:
                source.file_id = None
                source.state = 'error'
                print(f"No matching file found for source {source.name}")
                
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
        logging.error(f"Source {source.name} has no associated file")
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
        file = File.query.get(source.file_id)
        if not file:
            logging.error(f"File {source.file_id} not found for source {source.name}")
            return None

        # Download file from S3
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

def get_source_file_header(account_settings, source, num_lines=1):
    """
    Download only the header (first n lines) of a source's CSV file from S3.
    
    Args:
        account_settings: Account settings containing AWS credentials
        source: Source object containing file information
        num_lines: Number of lines to read from the start (default=1)
    
    Returns: 
        String containing the first n lines of the CSV, or None if error
    """
    if not source.file_id:
        logging.error(f"Source {source.name} has no associated file")
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
        file = File.query.get(source.file_id)
        if not file:
            logging.error(f"File {source.file_id} not found for source {source.name}")
            return None

        # Download only the first part of the file using range request
        response = s3_client.get_object(
            Bucket=account_settings.bucket_name,
            Key=file.key,
            Range='bytes=0-8192'  # Get first 8KB which should be enough for headers
        )
        
        # Read content and get first n lines
        content = response['Body'].read().decode('utf-8')
        lines = content.split('\n')[:num_lines]
        return '\n'.join(lines)

    except Exception as e:
        logging.error(f"Error downloading header for source {source.name}: {e}")
        return None

def setup_aws_resources(admin_settings, new_bucket_name, new_user_name):
    """
    Creates AWS resources (S3 bucket and IAM user) for a new account.
    Uses admin credentials to create these resources.
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

        # 2. Apply bucket policy
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

        # 4. Create and attach user policy
        try:
            user_policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "s3:ListBucket"
                        ],
                        "Resource": f"arn:aws:s3:::{new_bucket_name}"
                    },
                    {
                        "Effect": "Allow",
                        "Action": [
                            "s3:GetObject",
                            "s3:PutObject",
                            "s3:DeleteObject"
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