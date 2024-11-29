import logging
from typing import Protocol, List
import re
import os
from .local_handler import LocalHandler

logger = logging.getLogger()

class FileHandler(Protocol):
    """Protocol for file handling operations"""
    def list_files(self, file_filter: str, bucket_name: str = None) -> List[str]:
        """List files matching the filter"""
        ...
    
    def read_csv(self, file_path: str, bucket_name: str = None) -> List[List[str]]:
        """Read CSV file and return rows"""
        ...
    
    def write_csv(self, file_path: str, rows: List[List[str]], bucket_name: str = None):
        """Write rows to CSV file"""
        ...

class FileProcessor:
    def __init__(self, handler: FileHandler):
        self.handler = handler
    
    def process_files(self, name: str, file_filter: str, include_columns: str,
                     data_points: int, tail_only: bool, bucket_name: str = None):
        """Process files according to configuration"""
        try:
            # Convert include_columns to list
            columns = [col.strip() for col in include_columns.split(',')]
            
            # Get matching files
            files = self.handler.list_files(file_filter, bucket_name)
            if not files:
                raise ValueError(f"No files found matching filter: {file_filter}")
            
            # Initialize source file with header
            # For S3: .hublink/source/{name}.csv will be a key in the bucket
            # For local: it will be relative to bucket_name directory
            source_path = f".hublink/source/{name}.csv"
            if bucket_name and isinstance(self.handler, LocalHandler):
                source_path = os.path.join(bucket_name, source_path)
            
            self.handler.write_csv(source_path, [columns], bucket_name)
            
            # Process each file
            total_rows = 0
            for file_path in files:
                try:
                    # Read and process file
                    rows = self.handler.read_csv(file_path, bucket_name)
                    if not rows:
                        continue
                        
                    # Find column indices
                    header = rows[0]
                    col_indices = []
                    for col in columns:
                        try:
                            idx = header.index(col)
                            col_indices.append(idx)
                        except ValueError:
                            logger.warning(f"Column {col} not found in {file_path}")
                            break
                    
                    if len(col_indices) != len(columns):
                        continue
                    
                    # Extract data rows
                    data = []
                    for row in rows[1:]:  # Skip header
                        if len(row) >= max(col_indices) + 1:
                            data.append([row[i] for i in col_indices])
                    
                    # Apply data_points limit if specified
                    if data_points > 0:
                        if tail_only:
                            # Take last n rows
                            data = data[-data_points:]
                        else:
                            # Calculate sampling interval
                            total_rows = len(data)
                            if total_rows > data_points:
                                # Calculate step size (round up to ensure we don't exceed data_points)
                                step = -(-total_rows // data_points)  # Ceiling division
                                # Take every nth row
                                data = data[::step][:data_points]  # Slice again to ensure we don't exceed data_points
                    
                    # Append to source file
                    if data:
                        self.handler.write_csv(source_path, data, bucket_name)
                        total_rows += len(data)
                    
                except Exception as e:
                    logger.error(f"Error processing file {file_path}: {str(e)}")
                    continue
            
            if total_rows == 0:
                raise ValueError("No data was processed")
                
            logger.info(f"Successfully processed {total_rows} rows for source {name}")
            
        except Exception as e:
            logger.error(f"Error in process_files: {str(e)}")
            raise 