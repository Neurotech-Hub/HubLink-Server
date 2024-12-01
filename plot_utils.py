import pandas as pd
from io import StringIO
from S3Manager import download_source_file
import logging
from collections import defaultdict

# Get logger for this module
logger = logging.getLogger(__name__)

def process_metric_plot(plot, settings):
    """Process data for a metric plot showing mean and std dev."""
    try:
        logger.info(f"Processing metric plot {plot.id}")
        
        # Download source data
        csv_content = download_source_file(settings, plot.source)
        if not csv_content:
            return {'error': 'No data available for this source'}

        # Parse CSV content using pandas
        df = pd.read_csv(StringIO(csv_content))
        
        # Ensure required columns exist
        required_columns = ['hublink_device_id', plot.y_column]
        if not all(col in df.columns for col in required_columns):
            missing_cols = [col for col in required_columns if col not in df.columns]
            return {'error': f'Required columns not found in data: {missing_cols}'}

        # Convert y_column to numeric and remove any invalid values
        df[plot.y_column] = pd.to_numeric(df[plot.y_column], errors='coerce')
        
        # Remove NaN values
        original_rows = len(df)
        df = df.dropna(subset=[plot.y_column])
        removed_rows = original_rows - len(df)
        if removed_rows > 0:
            logger.warning(f"Removed {removed_rows} rows with invalid data")
        
        # Calculate statistics for each device
        stats = df.groupby('hublink_device_id')[plot.y_column].agg(['mean', 'std']).reset_index()
        stats['std'] = stats['std'].fillna(0)
        
        device_ids = stats['hublink_device_id'].tolist()
        means = stats['mean'].tolist()
        stds = stats['std'].tolist()
        
        logger.info(f"Processed {len(device_ids)} devices")

        return {
            'device_ids': device_ids,
            'means': means,
            'stds': stds,
            'error': None
        }

    except Exception as e:
        logger.error(f"Error processing metric plot: {e}", exc_info=True)
        return {'error': f'Error processing plot data: {str(e)}'}

def process_timeseries_plot(plot, settings):
    """Process data for a timeseries plot."""
    try:
        logger.info(f"Processing timeseries plot {plot.id}")
        
        # Download source data
        csv_content = download_source_file(settings, plot.source)
        if not csv_content:
            return {'error': 'No data available for this source'}

        # Parse CSV content using pandas
        df = pd.read_csv(StringIO(csv_content))
        logger.info(f"Loaded data: {df.shape[0]} rows, {df.shape[1]} columns")
        
        # Convert timestamps with error handling
        try:
            df[plot.x_column] = pd.to_datetime(
                df[plot.x_column], 
                format='%m/%d/%Y %H:%M:%S',
                errors='coerce'
            )
        except Exception as e:
            logger.warning(f"Standard date parsing failed: {e}, attempting flexible parsing")
            df[plot.x_column] = pd.to_datetime(
                df[plot.x_column], 
                errors='coerce'
            )
        
        # Convert y_column to numeric and remove invalid values
        df[plot.y_column] = pd.to_numeric(df[plot.y_column], errors='coerce')
        
        # Remove rows with NaN values
        original_rows = len(df)
        df = df.dropna(subset=[plot.x_column, plot.y_column])
        removed_rows = original_rows - len(df)
        if removed_rows > 0:
            logger.warning(f"Removed {removed_rows} rows with invalid data")

        if len(df) == 0:
            return {'error': 'No valid data points after cleaning'}

        # Group by device_id
        unique_devices = df['hublink_device_id'].unique()
        logger.info(f"Processing {len(unique_devices)} devices")
        
        grouped_data = {}
        for device_id in unique_devices:
            device_df = df[df['hublink_device_id'] == device_id].sort_values(by=plot.x_column)
            if len(device_df) > 0:
                grouped_data[device_id] = {
                    'x': device_df[plot.x_column].dt.strftime('%Y-%m-%d %H:%M:%S').tolist(),
                    'y': device_df[plot.y_column].tolist()
                }

        if not grouped_data:
            return {'error': 'No valid data after grouping by device'}

        # Prepare return format
        device_ids = list(grouped_data.keys())
        x_data = [grouped_data[device]['x'] for device in device_ids]
        y_data = [grouped_data[device]['y'] for device in device_ids]

        logger.info(f"Successfully processed {len(device_ids)} devices")
        return {
            'x_data': x_data,
            'y_data': y_data,
            'device_ids': device_ids,
            'error': None
        }

    except Exception as e:
        logger.error(f"Error processing timeseries plot: {e}", exc_info=True)
        return {'error': f'Error processing plot data: {str(e)}'} 