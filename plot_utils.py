import plotly.express as px
import pandas as pd
from io import StringIO
import json
import logging
from S3Manager import download_source_file

# Get logger for this module
logger = logging.getLogger(__name__)

def process_metric_plot(plot, csv_content):
    try:
        logger.info(f"Processing metric plot {plot.id}")
        config = json.loads(plot.config)
        data_column = config['data_column']
        display_type = config.get('display', 'bar')
        
        # Parse CSV content using pandas
        df = pd.read_csv(StringIO(csv_content))
        
        if display_type == 'box':
            # Create box plot using Plotly Express
            fig = px.box(df, 
                        x='hublink_device_id',
                        y=data_column,
                        title=plot.name,
                        labels={
                            'hublink_device_id': 'Device',
                            data_column: config['data_column']
                        })
            
            # Update layout with transparent background
            fig.update_layout(
                showlegend=True,
                boxmode='group',
                plot_bgcolor='white',
                paper_bgcolor='white'
            )
            
            # Update axes to create a cleaner grid
            fig.update_xaxes(showgrid=True, gridwidth=1, gridcolor='LightGray')
            fig.update_yaxes(showgrid=True, gridwidth=1, gridcolor='LightGray')
            
            plot_json = fig.to_json()
            
            return {
                'plotly_json': plot_json,
                'error': None
            }
        else:
            # Original bar/table processing...
            stats = df.groupby('hublink_device_id')[data_column].agg(['mean', 'std']).reset_index()
            return {
                'device_ids': stats['hublink_device_id'].tolist(),
                'means': stats['mean'].tolist(),
                'stds': stats['std'].tolist(),
                'error': None
            }

    except Exception as e:
        logger.error(f"Error processing metric plot: {e}", exc_info=True)
        return {'error': f'Error processing plot data: {str(e)}'}

def process_timeseries_plot(plot, csv_content):
    """Process data for a timeseries plot."""
    try:
        logger.info(f"Processing timeseries plot {plot.id}")
        config = json.loads(plot.config)
        time_column = config['time_column']
        data_column = config['data_column']
        
        # Parse CSV content using pandas
        df = pd.read_csv(StringIO(csv_content))
        logger.info(f"Loaded data: {df.shape[0]} rows, {df.shape[1]} columns")
        
        # Convert timestamps with error handling
        try:
            df[time_column] = pd.to_datetime(df[time_column], format='%m/%d/%Y %H:%M:%S', errors='coerce')
        except Exception as e:
            logger.warning(f"Standard date parsing failed: {e}, attempting flexible parsing")
            df[time_column] = pd.to_datetime(df[time_column], errors='coerce')
        
        # Convert data column to numeric and remove invalid values
        df[data_column] = pd.to_numeric(df[data_column], errors='coerce')
        
        # Remove rows with NaN values
        df = df.dropna(subset=[time_column, data_column])
        
        if len(df) == 0:
            return {'error': 'No valid data points after cleaning'}

        # Sort the dataframe by device ID and timestamp
        df = df.sort_values(['hublink_device_id', time_column])

        # Create line plot using Plotly Express
        fig = px.line(df, 
                     x=time_column,
                     y=data_column,
                     color='hublink_device_id',
                     title=plot.name,
                     labels={
                         time_column: config['time_column'],
                         data_column: config['data_column'],
                         'hublink_device_id': 'Device'
                     })
        
        # Update layout with transparent background
        fig.update_layout(
            showlegend=True,
            plot_bgcolor='white',
            paper_bgcolor='white'
        )
        
        # Update axes to create a cleaner grid
        fig.update_xaxes(showgrid=True, gridwidth=1, gridcolor='LightGray')
        fig.update_yaxes(showgrid=True, gridwidth=1, gridcolor='LightGray')
        
        plot_json = fig.to_json()
        
        return {
            'plotly_json': plot_json,
            'error': None
        }

    except Exception as e:
        logger.error(f"Error processing timeseries plot: {e}", exc_info=True)
        return {'error': f'Error processing plot data: {str(e)}'} 