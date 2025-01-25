import plotly.express as px
import pandas as pd
from io import StringIO
import json
import logging
from S3Manager import download_source_file
from models import db
import plotly.graph_objects as go
import os

# Get logger for this module
logger = logging.getLogger(__name__)

def get_group_name(file_path, group_by_level):
    """
    Get the group name for a file path based on the grouping level.
    If the level points to a directory with only files, use the full file name.
    
    Args:
        file_path (str): The full file path
        group_by_level (int): The level to group by (0-based) or None for no grouping
    
    Returns:
        str: The group name to use
    """
    if group_by_level is None:
        return "All Data"
        
    parts = file_path.strip('/').split('/')
    
    # If requesting a level beyond what exists, use the deepest level
    if group_by_level >= len(parts):
        group_by_level = len(parts) - 1
        
    # Get the path up to the requested level
    group_path = '/'.join(parts[:group_by_level + 1])
    
    return group_path

def prepare_grouped_df(df, plot):
    """
    Prepare a DataFrame with proper grouping based on plot.group_by level.
    
    Args:
        df (pd.DataFrame): The input DataFrame
        plot: The plot object containing group_by and other settings
    
    Returns:
        pd.DataFrame: DataFrame with a 'group' column for plotting
    """
    try:
        if 'file_path' not in df.columns:
            logger.error("file_path column not found in DataFrame")
            return df
            
        # Add group column based on file paths
        df['group'] = df['file_path'].apply(lambda x: get_group_name(x, plot.group_by))
        
        return df
    except Exception as e:
        logger.error(f"Error preparing grouped DataFrame: {str(e)}")
        return df

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

def get_plot_title(plot):
    """Helper function to generate consistent plot titles"""
    if hasattr(plot, 'source') and plot.source:
        return f"{plot.name} ({plot.source.name})"
    return plot.name

def process_timeseries_plot(plot, csv_content):
    try:
        logger.info(f"Processing timeseries plot {plot.id}")
        config = json.loads(plot.config)
        x_data = plot.source.datetime_column if hasattr(plot, 'source') else None
        y_data = config['y_data']
        advanced_options = json.loads(plot.advanced) if plot.advanced else []
        should_accumulate = 'accumulate_values' in advanced_options
        
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
        
        # Convert y_data to numeric, handling non-numeric values
        df[y_data] = pd.to_numeric(df[y_data], errors='coerce')
        df = df.dropna(subset=[x_data, y_data])
        
        if len(df) == 0:
            return {'error': 'No valid data points after cleaning'}

        # Apply grouping if needed
        if plot.group_by:
            df = prepare_grouped_df(df, plot)
            # Sort by datetime within each group
            df = df.sort_values(['group', x_data])
        else:
            # Just sort by datetime if no grouping
            df = df.sort_values(x_data)
        
        # Handle accumulation if enabled
        if should_accumulate and plot.type == 'timeline':
            logger.info("Accumulating values across files")
            # Process each group separately
            groups = []
            for group_name, group_df in df.groupby('group'):
                # Sort by datetime
                group_df = group_df.sort_values(x_data)
                
                # Initialize accumulation
                last_value = 0
                accumulated_data = []
                current_file = None
                
                # Process each row
                for _, row in group_df.iterrows():
                    if current_file != row['file_path']:
                        # New file started
                        current_file = row['file_path']
                        if accumulated_data:  # If we have previous data
                            last_value = accumulated_data[-1]  # Use last accumulated value
                    
                    # Add to accumulated value
                    accumulated_value = last_value + row[y_data]
                    accumulated_data.append(accumulated_value)
                    last_value = accumulated_value
                
                # Update the group's data
                group_df[y_data] = accumulated_data
                groups.append(group_df)
            
            # Combine all groups back
            if groups:
                df = pd.concat(groups)
        
        # Create figure using go.Figure for more control
        fig = go.Figure()
        
        # Define a color sequence using Plotly's default colors
        colors = px.colors.qualitative.Plotly
        
        if plot.group_by:
            # Plot each group separately
            for idx, group in enumerate(sorted(df['group'].unique())):
                group_data = df[df['group'] == group]
                color = colors[idx % len(colors)]
                
                fig.add_trace(go.Scatter(
                    x=group_data[x_data],
                    y=group_data[y_data],
                    name=group,
                    mode='lines+markers',
                    marker=dict(size=6, opacity=0.7, color=color),
                    line=dict(width=2, shape='linear', color=color),
                    fill='tozeroy',
                    fillcolor=f'rgba{tuple(list(px.colors.hex_to_rgb(color)) + [0.1])}'
                ))
        else:
            # Plot single line for non-grouped data
            color = colors[0]
            fig.add_trace(go.Scatter(
                x=df[x_data],
                y=df[y_data],
                name=y_data,
                mode='lines+markers',
                marker=dict(size=6, opacity=0.7, color=color),
                line=dict(width=2, shape='linear', color=color),
                fill='tozeroy',
                fillcolor=f'rgba{tuple(list(px.colors.hex_to_rgb(color)) + [0.1])}'
            ))
        
        # Update layout
        layout = get_default_layout(get_plot_title(plot))
        layout.update({
            'xaxis_title': x_data,
            'yaxis_title': y_data
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
        
        if len(df) == 0:
            return {'error': 'No valid data points after cleaning'}
            
        # Apply grouping if needed
        if plot.group_by:
            df = prepare_grouped_df(df, plot)
        
        # Create figure
        fig = go.Figure()
        colors = px.colors.qualitative.Plotly
        
        if plot.group_by:
            # Plot each group as a separate box
            for idx, group in enumerate(sorted(df['group'].unique())):
                group_data = df[df['group'] == group]
                color = colors[idx % len(colors)]
                
                fig.add_trace(go.Box(
                    y=group_data[y_data],
                    name=group,
                    marker_color=color,
                    boxpoints='outliers'
                ))
        else:
            # Single box plot for all data
            fig.add_trace(go.Box(
                y=df[y_data],
                name=y_data,
                marker_color=colors[0],
                boxpoints='outliers'
            ))
        
        # Update layout
        layout = get_default_layout(get_plot_title(plot))
        layout.update({
            'yaxis_title': y_data,
            'showlegend': plot.group_by  # Only show legend if grouped
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
        df[y_data] = pd.to_numeric(df[y_data], errors='coerce')
        df = df.dropna(subset=[y_data])
        
        if len(df) == 0:
            return {'error': 'No valid data points after cleaning'}
            
        # Apply grouping if needed
        if plot.group_by:
            df = prepare_grouped_df(df, plot)
            # Calculate statistics by group
            stats = (df.groupby('group', observed=True)[y_data]
                    .agg(['mean', 'std', 'count'])
                    .round(2))
        else:
            # Calculate overall statistics
            stats = pd.DataFrame({
                'mean': [df[y_data].mean()],
                'std': [df[y_data].std()],
                'count': [len(df)]
            }, index=['all']).round(2)
        
        # Create figure
        fig = go.Figure()
        colors = px.colors.qualitative.Plotly
        
        # Add bars with error bars
        fig.add_trace(go.Bar(
            x=stats.index,
            y=stats['mean'],
            error_y=dict(
                type='data',
                array=stats['std'],
                visible=True
            ),
            marker_color=colors[0] if not plot.group_by else [colors[i % len(colors)] for i in range(len(stats))]
        ))
        
        # Update layout
        layout = get_default_layout(get_plot_title(plot))
        layout.update({
            'yaxis_title': y_data,
            'xaxis_title': 'Group' if plot.group_by else None,
            'showlegend': False
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
        
        if len(df) == 0:
            return {'error': 'No valid data points after cleaning'}
            
        # Apply grouping if needed
        if plot.group_by:
            df = prepare_grouped_df(df, plot)
            # Calculate statistics by group
            stats = (df.groupby('group', observed=True)[y_data]
                    .agg(['count', 'mean', 'std', 'min', 'max'])
                    .round(2))
        else:
            # Calculate overall statistics
            stats = pd.DataFrame({
                'count': [len(df)],
                'mean': [df[y_data].mean()],
                'std': [df[y_data].std()],
                'min': [df[y_data].min()],
                'max': [df[y_data].max()]
            }, index=['all']).round(2)
        
        # Create figure
        fig = go.Figure(data=[go.Table(
            header=dict(
                values=['Group' if plot.group_by else 'Metric'] + list(stats.columns),
                fill_color='#f0f0f0',  # Light gray
                align='left',
                font=dict(color='black', size=12)
            ),
            cells=dict(
                values=[stats.index] + [stats[col] for col in stats.columns],
                fill_color='white',
                align='left',
                font=dict(color='#333333', size=12)  # Dark gray text
            )
        )])
        
        # Update layout
        layout = get_default_layout(get_plot_title(plot))
        layout.update({
            'paper_bgcolor': 'white',
            'plot_bgcolor': 'white'
        })
        fig.update_layout(layout)
        
        return {
            'plotly_json': fig.to_json(),
            'error': None
        }
    except Exception as e:
        logger.error(f"Error processing table plot: {e}", exc_info=True)
        return {'error': f'Error processing plot data: {str(e)}'} 