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
        print(f"Starting to process metric plot {plot.id}")
        
        # Download source data
        print(f"Attempting to download data for plot {plot.id}")
        csv_content = download_source_file(settings, plot.source)
        print(f"Download complete. CSV content exists: {bool(csv_content)}")
        
        if not csv_content:
            print("No CSV content returned from download_source_file")
            return {'error': 'No data available for this source'}

        # Parse CSV content using pandas
        print("Parsing CSV content")
        df = pd.read_csv(StringIO(csv_content))
        print(f"DataFrame shape: {df.shape}")
        print(f"DataFrame columns: {df.columns.tolist()}")
        
        # Ensure required columns exist
        required_columns = ['hublink_device_id', plot.y_column]  # Only need device_id and data column
        print(f"Checking for required columns: {required_columns}")
        if not all(col in df.columns for col in required_columns):
            missing_cols = [col for col in required_columns if col not in df.columns]
            print(f"Missing columns: {missing_cols}")
            return {'error': f'Required columns not found in data: {missing_cols}'}

        # Convert y_column to numeric and remove any invalid values
        logger.info(f"Converting {plot.y_column} to numeric")
        logger.info(f"Sample of data before conversion: {df[plot.y_column].head()}")
        df[plot.y_column] = pd.to_numeric(df[plot.y_column], errors='coerce')
        
        # Remove NaN values
        original_rows = len(df)
        df = df.dropna(subset=[plot.y_column])
        removed_rows = original_rows - len(df)
        if removed_rows > 0:
            logger.warning(f"Removed {removed_rows} rows with invalid data")
        
        # Calculate statistics for each device
        stats = df.groupby('hublink_device_id')[plot.y_column].agg(['mean', 'std']).reset_index()
        
        # Handle cases where std is NaN (single value)
        stats['std'] = stats['std'].fillna(0)
        
        device_ids = stats['hublink_device_id'].tolist()
        means = stats['mean'].tolist()
        stds = stats['std'].tolist()
        
        logger.info("Final statistics:")
        for device, mean, std in zip(device_ids, means, stds):
            logger.info(f"Device {device}: mean={mean:.2f}, std={std:.2f}")

        return {
            'device_ids': device_ids,
            'means': means,
            'stds': stds,
            'error': None
        }

    except Exception as e:
        logger.error(f"Error processing metric plot: {e}")
        logger.error(f"Traceback:", exc_info=True)
        return {'error': f'Error processing plot data: {str(e)}'}

def process_timeseries_plot(plot, settings):
    """Process data for a timeseries plot."""
    try:
        print(f"\nProcessing timeseries plot {plot.id}")
        
        # Download source data
        csv_content = download_source_file(settings, plot.source)
        if not csv_content:
            return {'error': 'No data available for this source'}

        # Parse CSV content using pandas
        df = pd.read_csv(StringIO(csv_content))
        print(f"\nDataFrame shape: {df.shape}")
        print(f"Columns: {df.columns.tolist()}")
        
        # Convert timestamps with error handling
        print(f"\nConverting {plot.x_column} to datetime")
        try:
            # First attempt with standard format
            df[plot.x_column] = pd.to_datetime(
                df[plot.x_column], 
                format='%m/%d/%Y %H:%M:%S',
                errors='coerce'  # This will set invalid dates to NaT
            )
        except Exception as e:
            print(f"Error with standard date parsing: {e}")
            # Fallback to flexible parsing
            df[plot.x_column] = pd.to_datetime(
                df[plot.x_column], 
                errors='coerce'
            )
        
        # Convert y_column to numeric and remove invalid values
        df[plot.y_column] = pd.to_numeric(df[plot.y_column], errors='coerce')
        
        # Remove rows with NaN values in either x or y columns
        original_rows = len(df)
        df = df.dropna(subset=[plot.x_column, plot.y_column])
        removed_rows = original_rows - len(df)
        if removed_rows > 0:
            print(f"Removed {removed_rows} rows with invalid data")
            print(f"Remaining rows: {len(df)}")

        if len(df) == 0:
            return {'error': 'No valid data points after cleaning'}

        # Group by device_id
        unique_devices = df['hublink_device_id'].unique()
        print(f"\nFound {len(unique_devices)} unique devices: {unique_devices.tolist()}")
        
        grouped_data = {}
        for device_id in unique_devices:
            device_df = df[df['hublink_device_id'] == device_id].sort_values(by=plot.x_column)
            if len(device_df) > 0:  # Only process if we have data
                grouped_data[device_id] = {
                    'x': device_df[plot.x_column].dt.strftime('%Y-%m-%d %H:%M:%S').tolist(),
                    'y': device_df[plot.y_column].tolist()
                }
                print(f"\nDevice {device_id}:")
                print(f"Number of points: {len(grouped_data[device_id]['x'])}")
                if len(grouped_data[device_id]['x']) > 0:
                    print(f"First timestamp: {grouped_data[device_id]['x'][0]}")
                    print(f"Last timestamp: {grouped_data[device_id]['x'][-1]}")
                    print(f"Y value range: {min(grouped_data[device_id]['y'])} to {max(grouped_data[device_id]['y'])}")

        if not grouped_data:
            return {'error': 'No valid data after grouping by device'}

        # Prepare return format
        device_ids = list(grouped_data.keys())
        x_data = [grouped_data[device]['x'] for device in device_ids]
        y_data = [grouped_data[device]['y'] for device in device_ids]

        print("\nFinal data structure:")
        print(f"Number of devices: {len(device_ids)}")
        print(f"Device IDs: {device_ids}")
        print(f"Data points per device: {[len(x) for x in x_data]}")

        return {
            'x_data': x_data,
            'y_data': y_data,
            'device_ids': device_ids,
            'error': None
        }

    except Exception as e:
        logger.error(f"Error processing timeseries plot: {e}")
        logger.error(f"Traceback:", exc_info=True)
        return {'error': f'Error processing plot data: {str(e)}'} 