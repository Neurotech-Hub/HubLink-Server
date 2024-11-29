import os
import csv
import glob
from typing import List
import logging

logger = logging.getLogger()

class LocalHandler:
    def list_files(self, file_filter: str, bucket_name: str = None) -> List[str]:
        """List local files matching the filter pattern"""
        try:
            # Use bucket_name as base path if provided
            base_path = bucket_name if bucket_name else '.'
            pattern = os.path.join(base_path, file_filter)
            
            # Get matching files
            matching_files = []
            for file_path in glob.glob(pattern):
                if os.path.isfile(file_path) and not os.path.basename(file_path).startswith('.'):
                    matching_files.append(file_path)
            
            return matching_files
            
        except Exception as e:
            logger.error(f"Error listing local files: {str(e)}")
            raise
    
    def read_csv(self, file_path: str, bucket_name: str = None) -> List[List[str]]:
        """Read local CSV file"""
        try:
            with open(file_path, 'r', newline='') as f:
                reader = csv.reader(f)
                return list(reader)
                
        except Exception as e:
            logger.error(f"Error reading local file {file_path}: {str(e)}")
            raise
    
    def write_csv(self, file_path: str, rows: List[List[str]], bucket_name: str = None):
        """Write rows to local CSV file"""
        try:
            # Create directory if it doesn't exist
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            
            # Append mode if file exists and rows is not header
            mode = 'a' if os.path.exists(file_path) and len(rows) > 1 else 'w'
            
            with open(file_path, mode, newline='') as f:
                writer = csv.writer(f)
                writer.writerows(rows)
                
        except Exception as e:
            logger.error(f"Error writing local file {file_path}: {str(e)}")
            raise 