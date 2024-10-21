import boto3
import logging
import os

def get_recent_files(bucket_name, aws_access_key_id, aws_secret_access_key, retries=3):
    try:
        # Create S3 client using the access key provided by the user
        s3_client = boto3.client(
            's3',
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name=os.getenv('AWS_REGION', 'us-east-1')  # Default to 'us-east-1' if not set
        )
    except Exception as e:
        logging.error(f"Failed to create S3 client: {e}")
        return []

    for attempt in range(retries):
        try:
            response = s3_client.list_objects_v2(Bucket=bucket_name, MaxKeys=30, Prefix='')
            files = []
            if 'Contents' in response:
                for obj in response['Contents']:
                    pre_signed_url = s3_client.generate_presigned_url(
                        'get_object',
                        Params={'Bucket': bucket_name, 'Key': obj['Key']},
                        ExpiresIn=3600  # URL valid for 1 hour
                    )
                    files.append({
                        'name': obj['Key'],
                        'url': pre_signed_url,
                        'size': obj['Size'],
                        'updated_at': obj['LastModified'].isoformat()
                    })
            return files
        except s3_client.exceptions.NoSuchBucket:
            logging.error(f"Bucket '{bucket_name}' does not exist.")
            return []
        except s3_client.exceptions.ClientError as e:
            error_code = e.response['Error']['Code']
            logging.error(f"Client error ({error_code}) accessing S3 bucket: {e}")
            if error_code == 'AccessDenied':
                logging.error("Access to the bucket was denied.")
                return []
        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed accessing S3 bucket: {e}")
            if attempt < retries - 1:
                logging.debug("Retrying...")
            else:
                return []
    return []