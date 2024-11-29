import boto3
import csv
import io
import re
from typing import List
import logging

logger = logging.getLogger()

class S3Handler:
    def __init__(self):
        self.s3_client = boto3.client('s3')
    
    def list_files(self, file_filter: str, bucket_name: str) -> List[str]:
        """List S3 files matching the filter pattern"""
        try:
            # Convert glob pattern to regex
            pattern = re.compile(file_filter.replace('*', '.*'))
            
            # Use paginator for large buckets
            paginator = self.s3_client.get_paginator('list_objects_v2')
            matching_files = []
            
            for page in paginator.paginate(Bucket=bucket_name):
                if 'Contents' not in page:
                    continue
                
                for obj in page['Contents']:
                    key = obj['Key']
                    if not key.startswith('.') and pattern.match(key):
                        matching_files.append(key)
            
            return matching_files
            
        except Exception as e:
            logger.error(f"Error listing S3 files: {str(e)}")
            raise
    
    def read_csv(self, file_path: str, bucket_name: str) -> List[List[str]]:
        """Read CSV file from S3"""
        try:
            response = self.s3_client.get_object(Bucket=bucket_name, Key=file_path)
            content = response['Body'].read().decode('utf-8')
            reader = csv.reader(io.StringIO(content))
            return list(reader)
            
        except Exception as e:
            logger.error(f"Error reading S3 file {file_path}: {str(e)}")
            raise
    
    def write_csv(self, file_path: str, rows: List[List[str]], bucket_name: str):
        """Write rows to CSV file in S3"""
        try:
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerows(rows)
            
            self.s3_client.put_object(
                Bucket=bucket_name,
                Key=file_path,
                Body=output.getvalue()
            )
            
        except Exception as e:
            logger.error(f"Error writing S3 file {file_path}: {str(e)}")
            raise 