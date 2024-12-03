import plotly.express as px
import pandas as pd
from io import StringIO
import json
import logging
from S3Manager import download_source_file
from models import db

# Get logger for this module
logger = logging.getLogger(__name__)

def get_plot_data(plot, source, account):
    try:
        csv_content = download_source_file(account.settings, source)
        if not csv_content:
            logger.error("Could not download source file")
            return {}

        if plot.type == 'metric':
            return process_metric_plot(plot, csv_content)
        elif plot.type == 'timeline':
            return process_timeseries_plot(plot, csv_content)
        else:
            return {}

    except Exception as e:
        logger.error(f"Error processing plot data: {str(e)}", exc_info=True)
        return {}

def get_plot_info(plot):
    config = json.loads(plot.config) if plot.config else {}
    
    try:
        # Load the stored plot data (which includes the plotly_json)
        plot_data = json.loads(plot.data) if plot.data else {}
        
        # If we have valid plotly_json in the stored data, use it
        if plot_data.get('plotly_json'):
            plotly_json = plot_data['plotly_json']
        else:
            # Only use empty data as a fallback if no valid plot data exists
            plotly_json = json.dumps({
                'data': [],
                'layout': get_default_layout(plot.name)
            })
            logger.warning(f"No plotly_json found for plot {plot.id}, using empty data")
    except Exception as e:
        logger.error(f"Error parsing plot data for plot {plot.id}: {e}")
        plotly_json = json.dumps({
            'data': [],
            'layout': get_default_layout(plot.name)
        })
    
    return {
        'plot_id': plot.id,
        'name': plot.name,
        'type': plot.type,
        'source_name': plot.source.name,
        'config': config,
        'plotly_json': plotly_json
    }

def get_default_layout(plot_name):
    return {
        'title': plot_name,
        'showlegend': True,
        'plot_bgcolor': 'white',
        'paper_bgcolor': 'white',
        'height': 400,
        'margin': {'l': 60, 'r': 30, 't': 60, 'b': 100},
        'legend': {
            'yanchor': "top",
            'y': 0.99,
            'xanchor': "left",
            'x': 0.01,
            'bgcolor': "rgba(255, 255, 255, 0.8)"
        },
        'xaxis': {
            'automargin': True,
            'showgrid': True,
            'gridwidth': 1,
            'gridcolor': 'LightGray'
        },
        'yaxis': {
            'automargin': True,
            'showgrid': True,
            'gridwidth': 1,
            'gridcolor': 'LightGray'
        }
    }

def process_metric_plot(plot, csv_content):
    try:
        logger.info(f"Processing metric plot {plot.id}")
        config = json.loads(plot.config)
        y_data = config['y_data']
        display_type = config.get('display', 'bar')
        
        df = pd.read_csv(StringIO(csv_content))
        
        if display_type == 'box':
            fig = px.box(df, 
                        x='hublink_device_id',
                        y=y_data,
                        labels={
                            'hublink_device_id': 'Device',
                            y_data: config['y_data']
                        })
            
            fig.update_layout(get_default_layout(plot.name))
            fig.update_layout(boxmode='group')
            
            return {
                'plotly_json': fig.to_json(),
                'error': None
            }
        else:
            stats = df.groupby('hublink_device_id')[y_data].agg(['mean', 'std']).reset_index()
            
            fig = px.bar(stats, 
                         x='hublink_device_id', 
                         y='mean', 
                         error_y='std',
                         labels={'hublink_device_id': 'Device', 'mean': 'Mean', 'std': 'Standard Deviation'})
            
            fig.update_layout(get_default_layout(plot.name))
            
            return {
                'plotly_json': fig.to_json(),
                'error': None
            }

    except Exception as e:
        logger.error(f"Error processing metric plot: {e}", exc_info=True)
        return {'error': f'Error processing plot data: {str(e)}'}

def process_timeseries_plot(plot, csv_content):
    try:
        logger.info(f"Processing timeseries plot {plot.id}")
        config = json.loads(plot.config)
        x_data = config['x_data']
        y_data = config['y_data']
        
        df = pd.read_csv(StringIO(csv_content))
        
        try:
            df[x_data] = pd.to_datetime(df[x_data], format='%m/%d/%Y %H:%M:%S', errors='coerce')
        except Exception as e:
            logger.warning(f"Standard date parsing failed: {e}, attempting flexible parsing")
            df[x_data] = pd.to_datetime(df[x_data], errors='coerce')
        
        df[y_data] = pd.to_numeric(df[y_data], errors='coerce')
        df = df.dropna(subset=[x_data, y_data])
        
        if len(df) == 0:
            return {'error': 'No valid data points after cleaning'}

        df = df.sort_values(['hublink_device_id', x_data])

        fig = px.line(df, 
                     x=x_data,
                     y=y_data,
                     color='hublink_device_id',
                     labels={
                         x_data: config['x_data'],
                         y_data: config['y_data'],
                         'hublink_device_id': 'Device'
                     })
        
        fig.update_layout(get_default_layout(plot.name))
        
        return {
            'plotly_json': fig.to_json(),
            'error': None
        }

    except Exception as e:
        logger.error(f"Error processing timeseries plot: {e}", exc_info=True)
        return {'error': f'Error processing plot data: {str(e)}'} 