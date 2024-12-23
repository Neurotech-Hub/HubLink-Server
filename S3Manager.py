import boto3
import logging
import os
from datetime import datetime, timedelta
from models import *
import json
from dateutil import parser

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