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
        # Generate plot data in real-time
        plot_data = get_plot_data(plot, plot.source, plot.source.account)
        
        if not plot_data:
            logger.warning(f"No plot data generated for plot {plot.id}")
            plotly_json = json.dumps({
                'data': [],
                'layout': get_default_layout(plot.name)
            })
        else:
            plotly_json = plot_data.get('plotly_json')
            if not plotly_json:
                logger.warning(f"No plotly_json in plot data for plot {plot.id}")
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
            'error': plot_data.get('error') if plot_data else None
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
        # For timeline plots, use the source's datetime_column instead of x_data from config
        x_data = plot.source.datetime_column
        y_data = config['y_data']
        
        if not x_data:
            return {'error': 'No datetime column configured for this source'}
        
        df = pd.read_csv(StringIO(csv_content))
        logger.debug(f"DataFrame shape: {df.shape}")
        logger.debug(f"Available columns: {df.columns.tolist()}")
        
        try:
            df[x_data] = pd.to_datetime(df[x_data], errors='coerce')
            logger.debug(f"Successfully parsed datetime column {x_data}")
        except Exception as e:
            logger.error(f"Failed to parse datetime column {x_data}: {str(e)}")
            return {'error': f'Could not parse datetime column {x_data}'}
        
        df[y_data] = pd.to_numeric(df[y_data], errors='coerce')
        df = df.dropna(subset=[x_data, y_data])
        
        if len(df) == 0:
            return {'error': 'No valid data points after cleaning'}

        # Check if device column exists
        device_col = 'hublink_device_id'
        if device_col not in df.columns:
            logger.warning(f"Column {device_col} not found in DataFrame")
            device_col = df.columns[0]  # Use first column as fallback
            logger.info(f"Using {device_col} as device column instead")

        # Pre-process the data to avoid Plotly's internal grouping
        df = df.sort_values([device_col, x_data])
        df[device_col] = df[device_col].astype(str)  # Convert to string to avoid categorical warnings
        
        # Create figure using go.Figure for more control
        fig = go.Figure()
        
        # Define a color sequence using Plotly's default colors
        colors = px.colors.qualitative.Plotly
        
        for idx, device in enumerate(sorted(df[device_col].unique())):
            device_data = df[df[device_col] == device]
            color = colors[idx % len(colors)]
            
            fig.add_trace(go.Scatter(
                x=device_data[x_data],
                y=device_data[y_data],
                name=device,
                mode='lines+markers',
                marker=dict(size=6, opacity=0.7, color=color),
                line=dict(width=2, shape='linear', color=color),
                fill='tozeroy',
                fillcolor=f'rgba{tuple(list(px.colors.hex_to_rgb(color)) + [0.1])}'
            ))
        
        # Update layout
        layout = get_default_layout(plot.name)
        layout.update({
            'xaxis_title': config['x_data'],
            'yaxis_title': config['y_data']
        })
        fig.update_layout(layout)
        
        return {
            'plotly_json': fig.to_json(),
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
        
        device_col = 'hublink_device_id'
        df[device_col] = df[device_col].astype(str)  # Convert to string to avoid categorical warnings
        
        # Create figure using go.Figure
        fig = go.Figure()
        
        # Define a color sequence using Plotly's default colors
        colors = px.colors.qualitative.Plotly
        
        # Add box plots with different colors for each device
        for idx, device in enumerate(sorted(df[device_col].unique())):
            device_data = df[df[device_col] == device]
            color = colors[idx % len(colors)]
            
            fig.add_trace(go.Box(
                y=device_data[y_data],
                name=device,
                marker_color=color,
                line=dict(color=color),
                boxmean=True  # adds mean marker
            ))
        
        # Update layout
        layout = get_default_layout(plot.name)
        layout.update({
            'xaxis_title': 'Device',
            'yaxis_title': config['y_data'],
            'boxmode': 'group'
        })
        fig.update_layout(layout)
        
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
        logger.debug(f"Available columns: {df.columns.tolist()}")
        
        df[y_data] = pd.to_numeric(df[y_data], errors='coerce')
        df = df.dropna(subset=[y_data])
        
        # Check if device column exists
        device_col = 'hublink_device_id'
        if device_col not in df.columns:
            logger.warning(f"Column {device_col} not found in DataFrame")
            device_col = df.columns[0]  # Use first column as fallback
            logger.info(f"Using {device_col} as device column instead")
        
        # Pre-process data to avoid Plotly's internal grouping
        stats = (df.groupby(device_col, observed=True)[y_data]
                .agg(['mean', 'std'])
                .reset_index())
        stats[device_col] = stats[device_col].astype(str)  # Convert to string to avoid categorical warnings
        
        # Create figure using go.Figure
        fig = go.Figure()
        
        # Define a color sequence using Plotly's default colors
        colors = px.colors.qualitative.Plotly
        
        # Add bars with different colors for each device
        for idx, device in enumerate(sorted(stats[device_col])):
            color = colors[idx % len(colors)]
            fig.add_trace(go.Bar(
                x=[device],
                y=[stats.loc[stats[device_col] == device, 'mean'].iloc[0]],
                error_y=dict(
                    type='data',
                    array=[stats.loc[stats[device_col] == device, 'std'].iloc[0]],
                    visible=True
                ),
                name=device,
                marker_color=color,
                showlegend=True
            ))
        
        # Update layout
        layout = get_default_layout(plot.name)
        layout.update({
            'xaxis_title': 'Device',
            'yaxis_title': f'{y_data} (Mean)',
            'barmode': 'group'
        })
        fig.update_layout(layout)
        
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
        
        # Convert to categorical and use proper groupby parameters
        df['hublink_device_id'] = pd.Categorical(df['hublink_device_id'])
        stats = (df.groupby('hublink_device_id', observed=True, group_keys=True)[y_data]
                .agg([
                    'count',
                    'mean',
                    'std',
                    'min',
                    'max'
                ])
                .reset_index())
        
        # Round numeric columns to 3 decimal places
        numeric_cols = stats.select_dtypes(include=['float64']).columns
        stats[numeric_cols] = stats[numeric_cols].round(3)
        
        # Rename columns for display
        stats.columns = ['Device', 'Count', 'Mean', 'Std Dev', 'Min', 'Max']
        
        fig = go.Figure(data=[go.Table(
            header=dict(
                values=list(stats.columns),
                fill_color='#f0f0f0',  # Light gray for header
                align='left',
                font=dict(size=14)  # Increased header font size
            ),
            cells=dict(
                values=[stats[col] for col in stats.columns],
                fill_color='#ffffff',  # White for cells
                align='left',
                font=dict(size=13)  # Increased cell font size
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