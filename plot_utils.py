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

logger.info("Plot utils module initialized")

def get_group_name(file_path, group_by_level, preserve_full_name=False):
    """
    Get the group name for a file path based on the grouping level.
    If the level points to a directory with only files, use the full file name.
    
    Args:
        file_path (str): The full file path
        group_by_level (int): The level to group by (0-based) or None for no grouping
        preserve_full_name (bool): If True, don't truncate long names (useful for tables)
    
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
    
    # For long names, just take the last 20 characters with ellipsis
    # unless preserve_full_name is True
    if len(group_path) > 20 and not preserve_full_name:
        return f"...{group_path[-20:]}"
    
    return group_path

def prepare_grouped_df(df, plot, preserve_full_name=False):
    """
    Prepare a DataFrame with proper grouping based on plot.group_by level.
    
    Args:
        df (pd.DataFrame): The input DataFrame
        plot: The plot object containing group_by and other settings
        preserve_full_name (bool): If True, don't truncate long group names
    
    Returns:
        pd.DataFrame: DataFrame with a 'group' column for plotting
    """
    try:
        if 'file_path' not in df.columns:
            logger.error("file_path column not found in DataFrame")
            return df
            
        # Add group column based on file paths
        df['group'] = df['file_path'].apply(lambda x: get_group_name(x, plot.group_by, preserve_full_name))
        
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
        elif plot.type == 'timebin':
            return process_timebin_plot(plot, csv_content)
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

def get_plot_info(plot, source_data=None):
    # Get config using the new property
    config = plot.config_json
    
    try:
        # Use provided source data or fetch it if not provided
        if source_data is None:
            source_data = download_source_file(plot.source.account.settings, plot.source)
            
        if not source_data:
            logger.warning(f"No source data available for plot {plot.id}")
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
                'error': 'No source data available'
            }

        # Generate plot data using the source data
        if plot.type == 'timeline':
            plot_data = process_timeseries_plot(plot, source_data)
        elif plot.type == 'timebin':
            plot_data = process_timebin_plot(plot, source_data)
        elif plot.type == 'box':
            plot_data = process_box_plot(plot, source_data)
        elif plot.type == 'bar':
            plot_data = process_bar_plot(plot, source_data)
        elif plot.type == 'table':
            plot_data = process_table_plot(plot, source_data)
        else:
            plot_data = {}

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
        'margin': {'l': 60, 'r': 30, 't': 60, 'b': 120},
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
            'gridcolor': 'LightGray',
            'tickangle': 22,  # Angle the labels
            'tickfont': {'size': 10}  # Slightly smaller font
        },
        'yaxis': {
            'automargin': True,
            'showgrid': True,
            'gridwidth': 1,
            'gridcolor': 'LightGray'
        },
        'hoverlabel': {
            'namelength': -1  # Show full text in hover
        }
    }

def get_plot_title(plot):
    """Helper function to generate consistent plot titles"""
    if hasattr(plot, 'source') and plot.source:
        return f"{plot.name} ({plot.source.name})"
    return plot.name

def read_and_decimate_csv(csv_content, datetime_col, value_col, max_points=2000):
    """Read CSV with early decimation to reduce memory usage."""
    try:
        # Count total lines first
        total_lines = sum(1 for _ in StringIO(csv_content))
        
        # If file is small enough, read normally
        if total_lines <= max_points:
            df = pd.read_csv(StringIO(csv_content), low_memory=False)
            return df
            
        # Calculate skip rate for decimation
        skip_rows = max(1, total_lines // max_points)
        
        # Read only every nth row
        df = pd.read_csv(
            StringIO(csv_content),
            skiprows=lambda x: x > 0 and x % skip_rows != 0,
            low_memory=False
        )
        
        return df
    except Exception as e:
        logger.error(f"Error in read_and_decimate_csv: {e}")
        # Fallback to normal read if decimation fails
        return pd.read_csv(StringIO(csv_content), low_memory=False)

def process_timeseries_plot(plot, csv_content):
    try:
        logger.info(f"Processing timeseries plot {plot.id}")
        config = plot.config_json
        x_data = plot.source.datetime_column if hasattr(plot, 'source') else None
        y_data = config['y_data']
        advanced_options = plot.advanced_json
        should_accumulate = 'accumulate' in advanced_options
        
        if not x_data:
            return {'error': 'No datetime column configured for this source'}
        
        # Use early decimation during CSV reading
        df = read_and_decimate_csv(csv_content, x_data, y_data)
        logger.debug(f"DataFrame shape after early decimation: {df.shape}")
        
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
            
            if plot.group_by:
                # Process each group separately when grouping is enabled
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
                        new_value = last_value + row[y_data]
                        accumulated_data.append(new_value)
                    
                    # Update the group's data with accumulated values
                    group_df[y_data] = accumulated_data
                    groups.append(group_df)
                
                # Combine all groups back together
                df = pd.concat(groups)
            else:
                # Handle accumulation without grouping
                df = df.sort_values(x_data)
                last_value = 0
                accumulated_data = []
                current_file = None
                
                for _, row in df.iterrows():
                    if current_file != row['file_path']:
                        current_file = row['file_path']
                        if accumulated_data:
                            last_value = accumulated_data[-1]
                    new_value = last_value + row[y_data]
                    accumulated_data.append(new_value)
                
                df[y_data] = accumulated_data
        
        # Create figure using go.Figure for more control
        fig = go.Figure()
        colors = px.colors.qualitative.Plotly
        
        if plot.group_by:
            # Plot each group separately
            for idx, group in enumerate(sorted(df['group'].unique())):
                group_data = df[df['group'] == group]
                color = colors[idx % len(colors)]
                
                # Create custom hover text for each point
                hover_texts = []
                for _, row in group_data.iterrows():
                    date_str = row[x_data].strftime('%Y-%m-%d %H:%M:%S')
                    value = row[y_data]
                    file_path = row['file_path']
                    # Format file path - get just the filename if it's too long
                    if len(file_path) > 30:
                        file_parts = file_path.split('/')
                        file_path = ".../" + '/'.join(file_parts[-2:]) if len(file_parts) > 1 else file_parts[-1]
                    
                    hover_text = f"Group: {group}<br>Date: {date_str}<br>Value: {value:.2f}<br>File: {file_path}"
                    hover_texts.append(hover_text)
                
                fig.add_trace(go.Scatter(
                    x=group_data[x_data],
                    y=group_data[y_data],
                    name=group,
                    mode='lines+markers',
                    marker=dict(size=6, opacity=0.7, color=color),
                    line=dict(width=2, shape='linear', color=color),
                    fill='tozeroy',
                    fillcolor=f'rgba{tuple(list(px.colors.hex_to_rgb(color)) + [0.1])}',
                    hovertext=hover_texts,
                    hoverinfo='text'
                ))
        else:
            # Plot single line for non-grouped data
            color = colors[0]
            
            # Create custom hover text for each point
            hover_texts = []
            for _, row in df.iterrows():
                date_str = row[x_data].strftime('%Y-%m-%d %H:%M:%S')
                value = row[y_data]
                file_path = row['file_path']
                # Format file path - get just the filename if it's too long
                if len(file_path) > 30:
                    file_parts = file_path.split('/')
                    file_path = ".../" + '/'.join(file_parts[-2:]) if len(file_parts) > 1 else file_parts[-1]
                
                hover_text = f"Date: {date_str}<br>Value: {value:.2f}<br>File: {file_path}"
                hover_texts.append(hover_text)
            
            fig.add_trace(go.Scatter(
                x=df[x_data],
                y=df[y_data],
                name=y_data,
                mode='lines+markers',
                marker=dict(size=6, opacity=0.7, color=color),
                line=dict(width=2, shape='linear', color=color),
                fill='tozeroy',
                fillcolor=f'rgba{tuple(list(px.colors.hex_to_rgb(color)) + [0.1])}',
                hovertext=hover_texts,
                hoverinfo='text'
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
        config = plot.config_json
        y_data = config['y_data']
        
        df = pd.read_csv(StringIO(csv_content), low_memory=False)
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
            'showlegend': bool(plot.group_by)  # Convert to boolean - show legend only if grouped
        })
        fig.update_layout(layout)
        
        return {
            'plotly_json': fig.to_json(),
            'error': None
        }
    except Exception as e:
        logger.error(f"Error processing box plot {plot.id}: {str(e)}", exc_info=True)
        return {'error': f'Error processing plot data: {str(e)}'}

def process_bar_plot(plot, csv_content):
    try:
        logger.info(f"Processing bar plot {plot.id}")
        logger.info(f"Config type: {type(plot.config)}, Config value: {plot.config}")
        config = plot.config_json
        logger.info(f"Local config type: {type(config)}, Local config value: {config}")
        
        if not isinstance(config, dict):
            logger.error(f"Config is not a dictionary! Converting from: {config}")
            try:
                # Try to parse if it's a JSON string
                import json
                config = json.loads(config) if isinstance(config, str) else {}
                logger.info(f"Converted config: {config}")
            except Exception as e:
                logger.error(f"Failed to parse config: {e}")
                config = {}
        
        y_data = config.get('y_data')
        logger.info(f"Y-data value: {y_data}")
        if not y_data:
            logger.error("No y_data found in config")
            return {'error': 'No y_data configuration found'}
            
        # Get datetime column from the source
        x_data = plot.source.datetime_column if hasattr(plot, 'source') else None
        
        advanced_options = plot.advanced_json
        take_last_value = 'last_value' in advanced_options
        
        df = pd.read_csv(StringIO(csv_content), low_memory=False)
        
        # Convert datetime column if available
        if x_data and x_data in df.columns:
            try:
                df[x_data] = pd.to_datetime(df[x_data], errors='coerce')
                logger.debug(f"Successfully parsed datetime column {x_data}")
            except Exception as e:
                logger.error(f"Failed to parse datetime column {x_data}: {str(e)}")
        
        df[y_data] = pd.to_numeric(df[y_data], errors='coerce')
        df = df.dropna(subset=[y_data])
        
        if len(df) == 0:
            return {'error': 'No valid data points after cleaning'}
            
        # Apply grouping if needed
        if plot.group_by:
            df = prepare_grouped_df(df, plot)
            
            # Store last datetime for each group if available
            last_dates = {}
            if x_data and x_data in df.columns:
                for group_name, group_df in df.groupby('group'):
                    group_df = group_df.sort_values(x_data)
                    if not group_df.empty:
                        last_dates[group_name] = group_df[x_data].iloc[-1]
            
            if take_last_value:
                # Get the last value for each group
                stats = (df.groupby('group', observed=True)[y_data]
                        .last()  # Take last value
                        .to_frame()  # Convert Series to DataFrame
                        .rename(columns={y_data: 'value'}))  # Rename column for consistency
            else:
                # Calculate statistics by group as before
                stats = (df.groupby('group', observed=True)[y_data]
                        .agg(['mean', 'std', 'count'])
                        .round(2))
        else:
            # Get the last datetime if available
            last_date = None
            if x_data and x_data in df.columns:
                df = df.sort_values(x_data)
                if not df.empty:
                    last_date = df[x_data].iloc[-1]
            
            if take_last_value:
                # Get the last value overall
                stats = pd.DataFrame({
                    'value': [df[y_data].iloc[-1]]  # Take last value
                }, index=['all'])
            else:
                # Calculate overall statistics as before
                stats = pd.DataFrame({
                    'mean': [df[y_data].mean()],
                    'std': [df[y_data].std()],
                    'count': [len(df)]
                }, index=['all']).round(2)
        
        # Create figure
        fig = go.Figure()
        colors = px.colors.qualitative.Plotly
        
        # Create hover texts
        hover_texts = []
        
        if plot.group_by:
            for group in stats.index:
                if take_last_value:
                    value = stats.loc[group, 'value']
                    hover_text = f"Group: {group}<br>Value: {value:.2f}"
                else:
                    mean_val = stats.loc[group, 'mean']
                    std_val = stats.loc[group, 'std']
                    count_val = stats.loc[group, 'count']
                    hover_text = f"Group: {group}<br>Mean: {mean_val:.2f}<br>Std: {std_val:.2f}<br>Count: {count_val}"
                
                # Add date information if available
                if group in last_dates:
                    date_str = last_dates[group].strftime('%Y-%m-%d %H:%M:%S')
                    hover_text += f"<br>Last Date: {date_str}"
                
                hover_texts.append(hover_text)
        else:
            if take_last_value:
                value = stats.loc['all', 'value']
                hover_text = f"Value: {value:.2f}"
            else:
                mean_val = stats.loc['all', 'mean']
                std_val = stats.loc['all', 'std']
                count_val = stats.loc['all', 'count']
                hover_text = f"Mean: {mean_val:.2f}<br>Std: {std_val:.2f}<br>Count: {count_val}"
            
            # Add date information if available
            if last_date:
                date_str = last_date.strftime('%Y-%m-%d %H:%M:%S')
                hover_text += f"<br>Last Date: {date_str}"
            
            hover_texts = [hover_text]
        
        # Add bars with hover text
        if take_last_value:
            fig.add_trace(go.Bar(
                x=stats.index,
                y=stats['value'],
                marker_color=colors[0] if not plot.group_by else [colors[i % len(colors)] for i in range(len(stats))],
                hovertext=hover_texts,
                hoverinfo='text'
            ))
        else:
            fig.add_trace(go.Bar(
                x=stats.index,
                y=stats['mean'],
                error_y=dict(
                    type='data',
                    array=stats['std'],
                    visible=True
                ),
                marker_color=colors[0] if not plot.group_by else [colors[i % len(colors)] for i in range(len(stats))],
                hovertext=hover_texts,
                hoverinfo='text'
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
        config = plot.config_json
        y_data = config['y_data']
        
        df = pd.read_csv(StringIO(csv_content), low_memory=False)
        df[y_data] = pd.to_numeric(df[y_data], errors='coerce')
        df = df.dropna(subset=[y_data])
        
        if len(df) == 0:
            return {'error': 'No valid data points after cleaning'}
            
        # Apply grouping if needed
        if plot.group_by:
            # Use preserve_full_name=True to show complete group names in tables
            df = prepare_grouped_df(df, plot, preserve_full_name=True)
            # Calculate statistics by group, including last value
            stats = (df.groupby('group', observed=True)[y_data]
                    .agg(['count', 'mean', 'std', 'min', 'max', ('last', 'last')])
                    .round(2))
        else:
            # Calculate overall statistics, including last value
            stats = pd.DataFrame({
                'count': [len(df)],
                'mean': [df[y_data].mean()],
                'std': [df[y_data].std()],
                'min': [df[y_data].min()],
                'max': [df[y_data].max()],
                'last': [df[y_data].iloc[-1]]
            }, index=['all']).round(3)
        
        # Reorder columns to put 'last' after 'count'
        stats = stats.reindex(columns=['count', 'last', 'mean', 'std', 'min', 'max'])
        
        # Determine column widths - allocate more width to the group column
        # Calculate max length of group names to determine width
        max_group_len = max([len(str(g)) for g in stats.index])
        
        # Adjust column widths based on content
        # Group column gets proportionally more width for longer text
        # For Plotly, we need to use relative numeric values, not percentages
        group_width = min(max(30, max_group_len * 2), 60)  # Increase max width to 60
        numeric_width = 10  # Default width for numeric columns
        
        # Set column widths as numeric values proportional to desired width
        # Use fixed values that match the proportions we want
        col_widths = [group_width] + [numeric_width] * len(stats.columns)
        
        # Format numeric values with fewer decimal places for better display
        formatted_values = []
        for col in stats.columns:
            if col in ['count']:
                # Integers don't need decimal places
                formatted_values.append(stats[col].astype(int))
            else:
                # Use 2 decimal places for other numerics
                formatted_values.append(stats[col].round(2))
        
        # Create figure with customized table
        fig = go.Figure(data=[go.Table(
            header=dict(
                values=['Group'] + list(stats.columns),
                fill_color='#f0f0f0',  # Light gray
                align=['left'] + ['right'] * len(stats.columns),  # Align group left, numbers right
                font=dict(color='black', size=12),
                height=35  # Slightly taller header for better readability
            ),
            cells=dict(
                values=[stats.index] + formatted_values,
                fill_color='white',
                align=['left'] + ['right'] * len(stats.columns),  # Align group left, numbers right
                font=dict(color='#333333', size=12),  # Dark gray text
                height=30,  # Consistent cell height
                format=None  # Let us handle formatting above
            ),
            columnwidth=col_widths
        )])
        
        # Update layout
        layout = get_default_layout(get_plot_title(plot))
        layout.update({
            'paper_bgcolor': 'white',
            'plot_bgcolor': 'white',
            'margin': {'t': 50, 'b': 30, 'l': 10, 'r': 10}  # Tighter margins for more table space
        })
        fig.update_layout(layout)
        
        return {
            'plotly_json': fig.to_json(),
            'error': None
        }
    except Exception as e:
        logger.error(f"Error processing table plot: {e}", exc_info=True)
        return {'error': f'Error processing plot data: {str(e)}'}

def process_timebin_plot(plot, csv_content):
    try:
        logger.info(f"Processing timebin plot {plot.id}")
        config = plot.config_json
        x_data = plot.source.datetime_column if hasattr(plot, 'source') else None
        y_data = config['y_data']
        bin_hrs = config.get('bin_hrs', 24)  # Default to 24 hours if not specified
        mean_nsum = config.get('mean_nsum', True)  # Default to mean if not specified
        
        # Validate bin_hrs parameter
        if not isinstance(bin_hrs, (int, float)) or bin_hrs <= 0:
            logger.warning(f"Invalid bin_hrs value: {bin_hrs}, using default 24")
            bin_hrs = 24
        if bin_hrs > 168:  # More than 1 week
            logger.warning(f"bin_hrs value {bin_hrs} is very large, this may cause performance issues")
        
        if not x_data:
            return {'error': 'No datetime column configured for this source'}
        
        # Read all data points for timebin plots to ensure accurate sum/mean calculations
        df = pd.read_csv(StringIO(csv_content), low_memory=False)
        logger.debug(f"DataFrame shape: {df.shape}")
        
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
        
        # Create time bins aligned to 00:00
        min_time = df[x_data].min()
        max_time = df[x_data].max()
        
        # Fix bin alignment logic
        start_time = min_time.normalize()
        if min_time.time() != pd.Timestamp('00:00:00').time():
            start_time -= pd.Timedelta(days=1)
            
        # Round up to next 00:00
        end_time = max_time.normalize() + pd.Timedelta(days=1)
        
        # Create bins every bin_hrs hours
        bins = pd.date_range(start=start_time, end=end_time, freq=f'{bin_hrs}h')
        
        # Debug bin creation
        logger.debug(f"Created {len(bins)} bins from {start_time} to {end_time} with {bin_hrs}h intervals")
        logger.debug(f"First few bins: {bins[:5]}")
        logger.debug(f"Last few bins: {bins[-5:]}")
        
        # Function to process a dataframe into bins
        def process_df_bins(df):
            # Create a copy of the dataframe to avoid SettingWithCopyWarning
            df_copy = df.copy()
            
            # Cut data into bins - use all bins for labels to avoid data loss
            df_copy.loc[:, 'bin'] = pd.cut(df_copy[x_data], bins=bins, labels=bins[:-1], include_lowest=True)
            
            # Remove any NaN bins (data outside the bin range)
            df_copy = df_copy.dropna(subset=['bin'])
            
            if len(df_copy) == 0:
                logger.warning("No data points fell within the bin range")
                return pd.Series(dtype=float), pd.Series(dtype=int)
            
            # Group by bin and calculate mean or sum, with observed=True to silence warning
            if mean_nsum:
                binned = df_copy.groupby('bin', observed=True)[y_data].mean()
            else:
                binned = df_copy.groupby('bin', observed=True)[y_data].sum()
                
            # Count points per bin for hover text
            counts = df_copy.groupby('bin', observed=True)[y_data].count()
            
            # Ensure binned and counts have the same index
            common_index = binned.index.intersection(counts.index)
            binned = binned.loc[common_index]
            counts = counts.loc[common_index]
            
            return binned, counts
        
        # Create figure
        fig = go.Figure()
        colors = px.colors.qualitative.Plotly
        
        if plot.group_by:
            # Process each group separately
            for idx, group in enumerate(sorted(df['group'].unique())):
                group_data = df[df['group'] == group].copy()  # Create a copy here
                binned, counts = process_df_bins(group_data)
                
                if len(binned) == 0:
                    logger.warning(f"No data for group {group} after binning")
                    continue
                    
                color = colors[idx % len(colors)]
                
                # Create hover text with proper index alignment
                hover_text = []
                for bin_time, value in binned.items():
                    count = counts.get(bin_time, 0)
                    hover_text.append(f"Group: {group}<br>"
                                   f"Time: {bin_time.strftime('%Y-%m-%d %H:%M')}<br>"
                                   f"{'Mean' if mean_nsum else 'Sum'}: {value:.2f}<br>"
                                   f"Points in bin: {count}")
                
                # Add lines with markers for this group
                fig.add_trace(go.Scatter(
                    x=binned.index,
                    y=binned.values,
                    name=group,
                    mode='lines+markers',
                    marker=dict(size=6, opacity=0.7, color=color),
                    line=dict(width=2, shape='linear', color=color),
                    fill='tozeroy',
                    fillcolor=f'rgba{tuple(list(px.colors.hex_to_rgb(color)) + [0.1])}',
                    hovertext=hover_text,
                    hoverinfo='text'
                ))
        else:
            # Process all data together
            binned, counts = process_df_bins(df)
            
            if len(binned) == 0:
                return {'error': 'No data points fell within the bin range'}
                
            color = colors[0]
            
            # Create hover text with proper index alignment
            hover_text = []
            for bin_time, value in binned.items():
                count = counts.get(bin_time, 0)
                hover_text.append(f"Time: {bin_time.strftime('%Y-%m-%d %H:%M')}<br>"
                               f"{'Mean' if mean_nsum else 'Sum'}: {value:.2f}<br>"
                               f"Points in bin: {count}")
            
            # Add single line with markers
            fig.add_trace(go.Scatter(
                x=binned.index,
                y=binned.values,
                name=y_data,
                mode='lines+markers',
                marker=dict(size=6, opacity=0.7, color=color),
                line=dict(width=2, shape='linear', color=color),
                fill='tozeroy',
                fillcolor=f'rgba{tuple(list(px.colors.hex_to_rgb(color)) + [0.1])}',
                hovertext=hover_text,
                hoverinfo='text'
            ))
        
        # Update layout
        layout = get_default_layout(get_plot_title(plot))
        layout.update({
            'xaxis_title': x_data,
            'yaxis_title': f"{y_data} ({'Mean' if mean_nsum else 'Sum'} per {bin_hrs}h bin)",
            'showlegend': bool(plot.group_by)  # Only show legend if grouped
        })
        fig.update_layout(layout)
        
        return {
            'plotly_json': fig.to_json(),
            'error': None
        }

    except Exception as e:
        logger.error(f"Error processing timebin plot: {e}", exc_info=True)
        return {'error': f'Error processing plot data: {str(e)}'} 