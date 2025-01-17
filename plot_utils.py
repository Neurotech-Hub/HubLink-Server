import plotly.express as px
import pandas as pd
from io import StringIO
import json
import logging
from S3Manager import download_source_file
from models import db
import plotly.graph_objects as go

# Get logger for this module
logger = logging.getLogger(__name__)

def get_plot_data(plot, source, account):
    try:
        csv_content = download_source_file(account.settings, source)
        if not csv_content:
            logger.error("Could not download source file")
            return {}

        logger.debug(f"CSV content first 100 chars: {csv_content[:100]}")  # Debug CSV content

        if plot.type == 'timeline':
            return process_timeseries_plot(plot, csv_content)
        elif plot.type == 'box':
            return process_box_plot(plot, csv_content)
        elif plot.type == 'bar':
            return process_bar_plot(plot, csv_content)
        elif plot.type == 'table':
            return process_table_plot(plot, csv_content)
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
        logger.debug(f"Plot {plot.id} data: {plot_data}")
        
        # If we have valid plotly_json in the stored data, use it
        if plot_data.get('plotly_json'):
            plotly_json = plot_data['plotly_json']
            logger.debug(f"Found plotly_json for plot {plot.id}")
        else:
            logger.warning(f"No plotly_json found for plot {plot.id}, using empty data")
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
            'plotly_json': plotly_json,
            'source_data': plot_data.get('source_data'),
            'error': plot_data.get('error')
        }
    except Exception as e:
        logger.error(f"Error getting plot info: {e}", exc_info=True)
        return None

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

def process_timeseries_plot(plot, csv_content):
    try:
        logger.info(f"Processing timeseries plot {plot.id}")
        config = json.loads(plot.config)
        x_data = config['x_data']
        y_data = config['y_data']
        
        df = pd.read_csv(StringIO(csv_content))
        logger.debug(f"DataFrame shape: {df.shape}")
        logger.debug(f"DataFrame columns: {df.columns.tolist()}")
        
        # Try to parse datetime with pandas' flexible parser first
        try:
            df[x_data] = pd.to_datetime(df[x_data], errors='coerce')
            logger.debug(f"Successfully parsed datetime column {x_data} using flexible parser")
        except Exception as e:
            logger.error(f"Failed to parse datetime column {x_data}: {str(e)}")
            return {'error': f'Could not parse datetime column {x_data}'}
        
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
        
        # Add markers and customize appearance for timeline
        fig.update_traces(
            mode='lines+markers',
            marker=dict(
                size=6,
                opacity=0.7
            ),
            line=dict(
                color='rgb(75, 192, 192)',  # Match dashboard.html color
                width=2,
                shape='spline'
            ),
            fill='tozeroy',
            fillcolor='rgba(75, 192, 192, 0.2)'  # Match dashboard.html fill color
        )
        
        fig.update_layout(get_default_layout(plot.name))
        
        # Wrap the plotly JSON in the expected format
        plotly_json = fig.to_json()
        return {
            'plotly_json': plotly_json,
            'error': None
        }

    except Exception as e:
        logger.error(f"Error processing timeseries plot: {e}", exc_info=True)
        return {'error': f'Error processing plot data: {str(e)}'}

def process_box_plot(plot, csv_content):
    try:
        logger.info(f"Processing box plot {plot.id}")
        config = json.loads(plot.config)
        y_data = config['y_data']
        
        df = pd.read_csv(StringIO(csv_content))
        df[y_data] = pd.to_numeric(df[y_data], errors='coerce')
        df = df.dropna(subset=[y_data])
        
        fig = px.box(df, 
                    x='hublink_device_id',
                    y=y_data,
                    color='hublink_device_id',
                    labels={
                        'hublink_device_id': 'Device',
                        y_data: config['y_data']
                    })
        
        # Update box appearance with blue color scheme
        fig.update_traces(
            marker=dict(
                color='rgba(54, 162, 235, 0.6)',  # Light blue at 0.6 opacity
                line=dict(
                    color='rgba(54, 162, 235, 1)',  # Solid border
                    width=1
                )
            ),
            line=dict(
                color='rgba(54, 162, 235, 1)'  # Solid border for whiskers and median line
            )
        )
        
        fig.update_layout(get_default_layout(plot.name))
        fig.update_layout(boxmode='group')
        
        return {
            'plotly_json': fig.to_json(),
            'error': None
        }

    except Exception as e:
        logger.error(f"Error processing box plot: {e}", exc_info=True)
        return {'error': f'Error processing plot data: {str(e)}'}

def process_bar_plot(plot, csv_content):
    try:
        logger.info(f"Processing bar plot {plot.id}")
        config = json.loads(plot.config)
        y_data = config['y_data']
        
        df = pd.read_csv(StringIO(csv_content))
        df[y_data] = pd.to_numeric(df[y_data], errors='coerce')
        df = df.dropna(subset=[y_data])
        
        stats = df.groupby('hublink_device_id')[y_data].agg(['mean', 'std']).reset_index()
        
        fig = px.bar(stats, 
                     x='hublink_device_id', 
                     y='mean', 
                     error_y='std',
                     color='hublink_device_id',
                     labels={
                         'hublink_device_id': 'Device',
                         'mean': f'{y_data} (Mean)',
                         'std': 'Standard Deviation'
                     })
        
        # Update bar appearance to exactly match dashboard style
        fig.update_traces(
            marker=dict(
                color='rgba(153, 102, 255, 0.6)',  # Purple at 0.6 opacity
                line=dict(
                    color='rgba(153, 102, 255, 1)',  # Solid border
                    width=1
                )
            )
        )
        
        fig.update_layout(get_default_layout(plot.name))
        
        return {
            'plotly_json': fig.to_json(),
            'error': None
        }

    except Exception as e:
        logger.error(f"Error processing bar plot: {e}", exc_info=True)
        return {'error': f'Error processing plot data: {str(e)}'}

def process_table_plot(plot, csv_content):
    try:
        logger.info(f"Processing table plot {plot.id}")
        config = json.loads(plot.config)
        y_data = config['y_data']
        
        df = pd.read_csv(StringIO(csv_content))
        df[y_data] = pd.to_numeric(df[y_data], errors='coerce')
        df = df.dropna(subset=[y_data])
        
        stats = df.groupby('hublink_device_id')[y_data].agg([
            'count',
            'mean',
            'std',
            'min',
            'max'
        ]).reset_index()
        
        # Round numeric columns to 3 decimal places
        numeric_cols = stats.select_dtypes(include=['float64']).columns
        stats[numeric_cols] = stats[numeric_cols].round(3)
        
        # Rename columns for display
        stats.columns = ['Device', 'Count', 'Mean', 'Std Dev', 'Min', 'Max']
        
        fig = go.Figure(data=[go.Table(
            header=dict(
                values=list(stats.columns),
                fill_color='#f0f0f0',  # Light gray for header
                align='left'
            ),
            cells=dict(
                values=[stats[col] for col in stats.columns],
                fill_color='#ffffff',  # White for cells
                align='left'
            )
        )])
        
        fig.update_layout(get_default_layout(plot.name))
        fig.update_layout(height=400)  # Adjust height for table
        
        return {
            'plotly_json': fig.to_json(),
            'error': None
        }

    except Exception as e:
        logger.error(f"Error processing table plot: {e}", exc_info=True)
        return {'error': f'Error processing plot data: {str(e)}'} 