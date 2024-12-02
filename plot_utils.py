import plotly.express as px
import pandas as pd
from io import StringIO
import json
import logging

# Get logger for this module
logger = logging.getLogger(__name__)

def process_metric_plot(plot, csv_content):
    try:
        logger.info(f"Processing metric plot {plot.id}")
        config = json.loads(plot.config)
        y_data = config['y_data']  # Use y_data consistently
        display_type = config.get('display', 'bar')
        
        # Parse CSV content using pandas
        df = pd.read_csv(StringIO(csv_content))
        
        if display_type == 'box':
            # Create box plot using Plotly Express
            fig = px.box(df, 
                        x='hublink_device_id',
                        y=y_data,
                        title=plot.name,
                        labels={
                            'hublink_device_id': 'Device',
                            y_data: config['y_data']
                        })
            
            # Update layout with transparent background
            fig.update_layout(
                showlegend=True,
                boxmode='group',
                plot_bgcolor='white',
                paper_bgcolor='white',
                margin=dict(
                    l=100,   # left margin
                    r=60,   # right margin
                    t=60,   # top margin
                    b=60    # bottom margin
                ),
                legend=dict(
                    yanchor="top",
                    y=0.99,
                    xanchor="left",
                    x=0.01,
                    bgcolor="rgba(255, 255, 255, 0.8)"
                ),
                xaxis=dict(
                    automargin=True,
                    showgrid=True,
                    gridwidth=1,
                    gridcolor='LightGray'
                ),
                yaxis=dict(
                    automargin=True,
                    showgrid=True,
                    gridwidth=1,
                    gridcolor='LightGray'
                )
            )
            
            plot_json = fig.to_json()
            
            return {
                'plotly_json': plot_json,
                'error': None
            }
        else:
            # Original bar/table processing...
            stats = df.groupby('hublink_device_id')[y_data].agg(['mean', 'std']).reset_index()
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
        x_data = config['x_data']  # Use x_data consistently
        y_data = config['y_data']  # Use y_data consistently
        
        # Parse CSV content using pandas
        df = pd.read_csv(StringIO(csv_content))
        logger.info(f"Loaded data: {df.shape[0]} rows, {df.shape[1]} columns")
        
        # Convert timestamps with error handling
        try:
            df[x_data] = pd.to_datetime(df[x_data], format='%m/%d/%Y %H:%M:%S', errors='coerce')
        except Exception as e:
            logger.warning(f"Standard date parsing failed: {e}, attempting flexible parsing")
            df[x_data] = pd.to_datetime(df[x_data], errors='coerce')
        
        # Convert data column to numeric and remove invalid values
        df[y_data] = pd.to_numeric(df[y_data], errors='coerce')
        
        # Remove rows with NaN values
        df = df.dropna(subset=[x_data, y_data])
        
        if len(df) == 0:
            return {'error': 'No valid data points after cleaning'}

        # Sort the dataframe by device ID and timestamp
        df = df.sort_values(['hublink_device_id', x_data])

        # Create line plot using Plotly Express
        fig = px.line(df, 
                     x=x_data,
                     y=y_data,
                     color='hublink_device_id',
                     title=plot.name,
                     labels={
                         x_data: config['x_data'],
                         y_data: config['y_data'],
                         'hublink_device_id': 'Device'
                     })
        
        # Update layout with transparent background
        fig.update_layout(
            showlegend=True,
            plot_bgcolor='white',
            paper_bgcolor='white',
            margin=dict(
                l=100,   # left margin
                r=60,   # right margin
                t=60,   # top margin
                b=60    # bottom margin
            ),
            legend=dict(
                yanchor="top",
                y=0.99,
                xanchor="left",
                x=0.01,
                bgcolor="rgba(255, 255, 255, 0.8)"
            ),
            xaxis=dict(
                automargin=True,
                showgrid=True,
                gridwidth=1,
                gridcolor='LightGray'
            ),
            yaxis=dict(
                automargin=True,
                showgrid=True,
                gridwidth=1,
                gridcolor='LightGray'
            )
        )
        
        plot_json = fig.to_json()
        
        return {
            'plotly_json': plot_json,
            'error': None
        }

    except Exception as e:
        logger.error(f"Error processing timeseries plot: {e}", exc_info=True)
        return {'error': f'Error processing plot data: {str(e)}'} 