import json
import logging
import requests
from typing import Dict, List
from file_processor import FileProcessor
from s3_handler import S3Handler
from local_handler import LocalHandler

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def update_status(name: str, status: str):
    """Send status update to HubLink"""
    try:
        response = requests.get(f"https://hublink.cloud/source/{name}/{status}")
        response.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to update status: {str(e)}")

def process_source(config: Dict, is_local: bool = False):
    """Process a single source configuration"""
    try:
        # Initialize appropriate handler
        handler = LocalHandler() if is_local else S3Handler()
        processor = FileProcessor(handler)
        
        # Process files and create source file
        processor.process_files(
            name=config['name'],
            file_filter=config.get('file_filter', '*'),
            include_columns=config.get('include_columns', '*'),
            data_points=config.get('data_points', 0),
            tail_only=config.get('tail_only', False),
            bucket_name=config.get('bucket_name')
        )
        
        update_status(config['name'], "success")
        return True
    except Exception as e:
        logger.error(f"Error processing source {config['name']}: {str(e)}")
        update_status(config['name'], "failure")
        return False

def lambda_handler(event, context):
    """AWS Lambda handler"""
    try:
        # Parse request body
        body = json.loads(event['body'])
        sources = body.get('sources', [])
        
        if not sources:
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'No sources provided'})
            }
        
        # Process each source
        results = []
        for source in sources:
            success = process_source(source)
            results.append({
                'name': source['name'],
                'success': success
            })
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Processing complete',
                'results': results
            })
        }
        
    except Exception as e:
        logger.error(f"Lambda handler error: {str(e)}")
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }

# Local testing
if __name__ == "__main__":
    # Example local test
    test_event = {
        'body': json.dumps({
            'sources': [{
                'name': 'test_source',
                'file_filter': '*.csv',
                'include_columns': 'timestamp,value',
                'data_points': 1000,
                'tail_only': False,
                'bucket_name': None  # Use local path for testing
            }]
        })
    }
    result = lambda_handler(test_event, None)
    print(json.dumps(result, indent=2))
