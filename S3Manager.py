import boto3
import logging
import os
from datetime import datetime, timedelta, timezone
from flask_sqlalchemy import SQLAlchemy
from models import File, Account, db, Setting
from sqlalchemy import update

def update_S3_files(account_settings, force_update=False):
    retries = 3
    account_id = account_settings.account_id

    # Check if the update has run in the past 5 minutes for this account
    account = Account.query.filter_by(id=account_id).first()
    if not force_update:
        if account and account.updated_at and datetime.now(timezone.utc) - account.updated_at.replace(tzinfo=timezone.utc) < timedelta(minutes=5):
            logging.info(f"Skipping update for account {account_id}, as it was already run in the last 5 minutes.")
            return

    # Validate that required settings are not empty
    if not account_settings.aws_access_key_id or not account_settings.aws_secret_access_key or not account_settings.bucket_name:
        logging.error(f"Missing AWS credentials or bucket name for account {account_id}.")
        return

    try:
        # Create S3 client using the access key provided by the account settings
        s3_client = boto3.client(
            's3',
            aws_access_key_id=account_settings.aws_access_key_id,
            aws_secret_access_key=account_settings.aws_secret_access_key,
            region_name=os.getenv('AWS_REGION', 'us-east-1')  # Default to 'us-east-1' if not set
        )
    except Exception as e:
        logging.error(f"Failed to create S3 client: {e}")
        return

    # Get the latest last_modified timestamp from the database for the given account
    latest_file = File.query.filter_by(account_id=account_id).order_by(File.last_modified.desc()).first()
    last_modified_cutoff = latest_file.last_modified.astimezone(timezone.utc) if latest_file else datetime.min.replace(tzinfo=timezone.utc)

    continuation_token = None

    for attempt in range(retries):
        try:
            while True:
                if continuation_token:
                    response = s3_client.list_objects_v2(
                        Bucket=account_settings.bucket_name, Prefix='', ContinuationToken=continuation_token
                    )
                else:
                    response = s3_client.list_objects_v2(Bucket=account_settings.bucket_name, Prefix='')

                if 'Contents' in response:
                    new_files = []
                    for obj in response['Contents']:
                        if obj['LastModified'].astimezone(timezone.utc) > last_modified_cutoff:
                            new_file = File(
                                account_id=account_id,
                                key=obj['Key'],
                                url=f"s3://{account_settings.bucket_name}/{obj['Key']}",  # Store the S3 path instead of presigned URL
                                size=obj['Size'],
                                last_modified=obj['LastModified']
                            )
                            new_files.append(new_file)

                    # Add new files to the session
                    if new_files:
                        db.session.bulk_save_objects(new_files)
                        db.session.commit()

                # Check if there are more files to list
                if response.get('IsTruncated'):
                    continuation_token = response.get('NextContinuationToken')
                else:
                    break

            # Update the last update timestamp for this account
            account.updated_at = datetime.now(timezone.utc)
            db.session.commit()
            return

        except s3_client.exceptions.NoSuchBucket:
            logging.error(f"Bucket '{account_settings.bucket_name}' does not exist.")
            return
        except s3_client.exceptions.ClientError as e:
            error_code = e.response['Error']['Code']
            logging.error(f"Client error ({error_code}) accessing S3 bucket: {e}")
            if error_code == 'AccessDenied':
                logging.error("Access to the bucket was denied.")
                return
        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed accessing S3 bucket: {e}")
            if attempt < retries - 1:
                logging.debug("Retrying...")
            else:
                return

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

def get_latest_files(account_id, total=30):
    try:
        latest_files = File.query.filter_by(account_id=account_id).order_by(File.last_modified.desc()).limit(total).all()
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