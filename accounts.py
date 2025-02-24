from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, g, send_file, current_app, session, abort
from models import db, Account, Setting, File, Gateway, Source, Plot, Layout, Node
from datetime import datetime, timedelta, timezone
import logging
from S3Manager import *
import traceback
from werkzeug.exceptions import BadRequest
import re
import requests
import os
import json
from plot_utils import get_plot_info, get_plot_data
import tempfile
import zipfile
from werkzeug.utils import secure_filename
import io
import shutil
from utils import admin_required, get_analytics, initiate_source_refresh, list_source_files, format_datetime
import dateutil.parser as parser
import time
from sqlalchemy import and_, not_
import pytz

# Create logger for this module
logger = logging.getLogger(__name__)

# Create the Blueprint for account-related routes
accounts_bp = Blueprint('accounts', __name__)

# Log module initialization
logger.info("Accounts module initialized")

@accounts_bp.before_request
def load_blueprint_user():
    g.user = None
    if 'admin_id' in session:
        account = db.session.get(Account, session['admin_id'])
        if account and account.is_admin:
            g.user = account

# Add template filter for datetime formatting
@accounts_bp.app_template_filter('format_datetime')
def _jinja2_filter_datetime(dt, tz_name='America/Chicago', format='relative'):
    return format_datetime(dt, tz_name, format)

# Route to view the account dashboard by its unique URL (page load)
@accounts_bp.route('/<account_url>', methods=['GET'])
def account_dashboard(account_url):
    g.title = "Dashboard"
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        
        return render_template('dashboard.html', account=account)
    except Exception as e:
        logging.error(f"Error loading dashboard for {account_url}: {e}")
        return "There was an issue loading the dashboard page.", 500

# Route to output account settings as JSON (gateway ping)
@accounts_bp.route('/<account_url>.json', methods=['GET'])
@accounts_bp.route('/<account_url>.json/<gateway_name>', methods=['GET'])
def get_account(account_url, gateway_name=None):
    g.title = "API"
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        account.count_gateway_pings += 1
        db.session.commit()
        setting = Setting.query.filter_by(account_id=account.id).first()
        if setting:
            # Extract client IP address
            ip_address = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
            if gateway_name:
                # Create new gateway
                gateway = Gateway(
                    account_id=account.id,
                    name=gateway_name,
                    ip_address=ip_address,
                    created_at=datetime.now(timezone.utc)
                )
                db.session.add(gateway)
                
                # Commit to get gateway ID
                db.session.commit()
            return jsonify(setting.to_dict())
        else:
            return jsonify({'error': 'Settings not found'}), 404
    except Exception as e:
        logging.error(f"Error generating JSON for {account_url}: {e}")
        return "There was an issue generating the JSON.", 500

# Route to view the settings for an account (page load)
@accounts_bp.route('/<account_url>/settings', methods=['GET'])
def account_settings(account_url):
    g.title = "Settings"
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        settings = Setting.query.filter_by(account_id=account.id).first_or_404()
        return render_template('settings.html', account=account, settings=settings)
    except Exception as e:
        logging.error(f"Error loading settings for {account_url}: {e}")
        return "There was an issue loading the settings page.", 500

# Route to submit updated settings for an account
@accounts_bp.route('/<account_url>/settings/update', methods=['POST'])
def update_settings(account_url):
    account = Account.query.filter_by(url=account_url).first_or_404()
    try:
        settings = Setting.query.filter_by(account_id=account.id).first_or_404()
        
        # Validate device_name_includes is not empty
        device_name_includes = request.form.get('device_name_includes', '').strip()
        if not device_name_includes:
            flash("Device Filter by Name cannot be empty", "error")
            return redirect(url_for('accounts.account_settings', account_url=account_url))

        # Validate max_file_size is 1GB or less
        max_file_size = int(request.form['max_file_size'])
        if max_file_size > 1073741824:  # 1GB in bytes
            flash("Max file size cannot exceed 1GB", "error")
            return redirect(url_for('accounts.account_settings', account_url=account_url))

        original_access_key = None
        original_secret_key = None
        original_bucket_name = None

        # Store original values only if admin
        if g.user and g.user.is_admin:
            original_access_key = settings.aws_access_key_id
            original_secret_key = settings.aws_secret_access_key
            original_bucket_name = settings.bucket_name
        else:
            # Non-admins can't modify AWS settings
            if (request.form.get('aws_access_key_id') != settings.aws_access_key_id or
                request.form.get('aws_secret_access_key') != settings.aws_secret_access_key or
                request.form.get('bucket_name') != settings.bucket_name):
                flash("Only administrators can modify AWS settings", "error")
                return redirect(url_for('accounts.account_settings', account_url=account_url))

        # Update settings with new form data
        settings.aws_access_key_id = request.form['aws_access_key_id']
        settings.aws_secret_access_key = request.form['aws_secret_access_key']
        settings.bucket_name = request.form['bucket_name']
        settings.max_file_size = max_file_size
        settings.use_cloud = request.form.get('use_cloud') == 'true'
        settings.gateway_manages_memory = request.form.get('gateway_manages_memory') == 'true'
        settings.device_name_includes = device_name_includes
        settings.timezone = request.form.get('timezone', 'America/Chicago')  # Add timezone with default

        # Check if AWS settings have changed
        aws_settings_changed = (
            original_access_key != settings.aws_access_key_id or
            original_secret_key != settings.aws_secret_access_key or
            original_bucket_name != settings.bucket_name
        )

        # we would need to rebuild if AWS settings changed

        db.session.commit()

        if aws_settings_changed:
            flash("AWS settings updated successfully!", "success")
        else:
            flash("Settings updated successfully!", "success")

        return redirect(url_for('accounts.account_settings', account_url=account_url))

    except Exception as e:
        db.session.rollback()
        logging.error(f"Error updating settings for {account_url}: {e}")
        flash("An error occurred while updating settings", "error")
        return redirect(url_for('accounts.account_settings', account_url=account_url))

def get_directory_paths(account_id):
    """Get unique directory paths that directly contain files for an account."""
    files = File.query.filter_by(account_id=account_id)\
        .filter(~File.key.like('.%'))\
        .filter(~File.key.contains('/.')) \
        .all()
    directories = set()
    
    for file in files:
        # Get the directory path by removing the file name
        path_parts = file.key.split('/')
        if len(path_parts) > 1:  # If file is in a directory
            # Join all parts except the last one (file name)
            dir_path = '/'.join(path_parts[:-1])
            if not any(part.startswith('.') for part in path_parts):  # Skip if any part is hidden
                directories.add(dir_path)
    
    # Convert to sorted list
    return sorted(list(directories))

@accounts_bp.route('/<account_url>/data', methods=['GET'])
@accounts_bp.route('/<account_url>/data/<path:directory>', methods=['GET'])
def account_data(account_url, directory=None):
    g.title = "Data"
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        return render_template('data.html', account=account, directory=directory)
    except Exception as e:
        logging.error(f"Error loading data for {account_url}: {e}")
        return "There was an issue loading the data page.", 500

@accounts_bp.route('/<account_url>/source/<int:source_id>.json', methods=['GET'])
def get_source_files(account_url, source_id):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        source = Source.query.filter_by(id=source_id, account_id=account.id).first_or_404()
            
        # Get matching files for this source
        matching_files = list_source_files(account, source)
        
        # Sort by last_modified in descending order (most recent first) and limit to 300
        matching_files.sort(key=lambda x: x.last_modified or datetime.min, reverse=True)
        matching_files = matching_files[:300]  # Limit to 300 most recent files
        
        # Filter files up to 12MB cumulative size
        filtered_files = []
        cumulative_size = 0
        max_size = 12 * 1024 * 1024  # 12MB in bytes
        
        for file in matching_files:
            if cumulative_size + file.size <= max_size:
                filtered_files.append(file)
                cumulative_size += file.size
            else:
                break
        
        # Convert files to dict for JSON response
        files_data = [{'key': f.key, 'size': f.size, 
                       'last_modified': f.last_modified.isoformat() if f.last_modified else None} 
                      for f in filtered_files]
        
        return jsonify(files_data)
        
    except Exception as e:
        logging.error(f"Error getting source files: {e}")
        return jsonify({'error': str(e)}), 500

def _update_sources_for_files(account, affected_files):
    """Helper function to update sources based on affected files.
    
    Args:
        account: Account object
        affected_files: List of File objects that were affected
        
    Returns:
        Tuple of (refresh_count, affected_sources)
    """
    if not affected_files:
        return 0, set()
        
    # Update account counter
    account.count_uploaded_files += len(affected_files)
    account.count_uploaded_files_mo += len(affected_files)
    
    # Get all sources for this account
    sources = Source.query.filter_by(account_id=account.id).all()
    affected_sources = set()  # Use set to ensure uniqueness
    
    # Create a set of affected file keys for efficient lookup
    affected_file_keys = {file.key for file in affected_files}
    
    # For each source, check if any of its matching files were affected
    for source in sources:
        try:
            matching_files = list_source_files(account, source)
            # Check if any matching files were affected
            if any(file.key in affected_file_keys for file in matching_files):
                affected_sources.add(source)
        except Exception as e:
            logging.error(f"Error matching pattern for source {source.id}: {e}")
            continue
    
    # Set do_update=True for each affected source
    refresh_count = 0
    for source in affected_sources:
        try:
            source.do_update = True
            refresh_count += 1
            logging.info(f"Marked {account.name}: {source.id}|{source.name} for update")
        except Exception as e:
            logging.error(f"Error marking source {source.id} for update: {e}")
            continue

    db.session.commit()
    logging.info(f"Added/updated {len(affected_files)} files; marked {refresh_count} sources for refresh for {account.id}|{account.name}")
    
    return refresh_count, affected_sources

@accounts_bp.route('/<account_url>/rebuild', methods=['GET'])
def rebuild(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        settings = Setting.query.filter_by(account_id=account.id).first_or_404()

        # Get list of affected files from rebuild operation
        affected_files = rebuild_S3_files(settings)
        
        # Update sources using helper function
        refresh_count, _ = _update_sources_for_files(account, affected_files)

        return jsonify({
            "message": "Rebuild completed successfully",
            "affected_files": len(affected_files) if affected_files else 0,
            "marked_for_refresh": refresh_count
        }), 200
    except Exception as e:
        logging.error(f"Error during '/rebuild' endpoint: {e}")
        return jsonify({"error": "There was an issue processing the rebuild request."}), 500

@accounts_bp.route('/<account_url>/download/<int:file_id>', methods=['GET'])
def download_file(account_url, file_id):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        account.count_file_downloads += 1
        db.session.commit()
        # Ensure the account exists
        account = Account.query.filter_by(url=account_url).first_or_404()
        
        # Ensure the file belongs to the given account
        file = File.query.filter_by(id=file_id, account_id=account.id).first_or_404()
        settings = Setting.query.filter_by(account_id=account.id).first_or_404()
        
        # Generate a download link using the settings and file key
        download_link = generate_download_link(settings, file.key)
        if not download_link:
            return "There was an issue generating the download link.", 500
        
        return redirect(download_link)
    except Exception as e:
        logging.error(f"Error downloading file {file_id} for account {account_url}: {e}")
        return "There was an issue downloading the file.", 500

# Define error handler
@accounts_bp.errorhandler(404)
def page_not_found(e):
    logging.error(f"404 error: {e}")
    return render_template('404.html'), 404

@accounts_bp.route('/<account_url>/plots', methods=['GET'])
def account_plots(account_url):
    g.title = "Plots"
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        
        # Get all sources and their path levels
        sources = account.sources
        source_paths = {}
        
        for source in sources:
            # Get files for this source
            files = list_source_files(account, source)
            if files:
                # Get the first file's path segments, including filename
                path_segments = files[0].key.split('/')
                # Create a list of truncated segments (now including the filename)
                truncated_segments = []
                for segment in path_segments:
                    if len(segment) > 8:
                        truncated = segment[:8] + "..."
                    else:
                        truncated = segment
                    truncated_segments.append({
                        'display': truncated,
                        'full': segment
                    })
                source_paths[source.id] = truncated_segments
        
        # Get layout plot names for display
        layout_plot_names = {}
        for layout in account.layouts:
            try:
                config = json.loads(layout.config)
                plot_names = []
                for item in config:
                    if 'plotId' in item:
                        plot = Plot.query.get(item['plotId'])
                        if plot:
                            plot_names.append(plot.name)
                layout_plot_names[layout.id] = plot_names
            except:
                layout_plot_names[layout.id] = []
        
        # Get directories for source creation
        directories = get_directory_paths(account.id)

        # Get recent files for the dropdown
        recent_files = File.query.filter_by(account_id=account.id)\
            .filter(~File.key.like('.%'))\
            .filter(~File.key.contains('/.')) \
            .order_by(File.last_modified.desc())\
            .limit(50)\
            .all()
        
        return render_template('plots.html', 
                             account=account, 
                             sources=sources,
                             source_paths=source_paths,
                             layout_plot_names=layout_plot_names,
                             directories=directories,
                             recent_files=recent_files)
                             
    except Exception as e:
        logging.error(f"Error loading plots for {account_url}: {e}")
        traceback.print_exc()
        return "There was an issue loading the plots page.", 500

def generate_directory_patterns(file_keys):
    """Get unique directory paths from file keys."""
    directories = {'/'}  # Start with root
    
    for key in file_keys:
        if key.startswith('.') or '/' not in key:
            continue
            
        # Split the path and build each level
        parts = key.split('/')
        current_path = ''
        for part in parts[:-1]:  # Exclude the filename
            if part.startswith('.'):
                break
            current_path = f"{current_path}/{part}" if current_path else f"/{part}"
            directories.add(current_path)
    
    # Convert to sorted list
    return sorted(list(directories))

@accounts_bp.route('/<account_url>/source/<int:source_id>/refresh', methods=['POST'])
def refresh_source(account_url, source_id):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        source = Source.query.filter_by(id=source_id, account_id=account.id).first_or_404()

        success, error = initiate_source_refresh(account, source)
        
        if not success:
            flash(f'Error refreshing source: {error}', 'error')
        
    except Exception as e:
        logging.error(f"Error in refresh_source route: {str(e)}")
        flash('Error refreshing source.', 'error')
    
    return redirect(url_for('accounts.account_plots', account_url=account_url))

@accounts_bp.route('/<account_url>/source', methods=['POST'])
def create_source(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        
        # Get form data
        source_id = request.form.get('source_id')
        name = request.form.get('name', '').strip()
        directory_filter = request.form.get('directory_filter', '')
        include_subdirs = request.form.get('include_subdirs') == 'on'
        include_columns = request.form.get('include_columns', '')
        datetime_column = request.form.get('datetime_column', '')
        tail_only = request.form.get('tail_only') == 'on'
        data_points = request.form.get('data_points', type=int)
        
        # Check if a source with this name already exists
        existing_source = Source.query.filter_by(account_id=account.id, name=name).first()
        if existing_source and (not source_id or int(source_id) != existing_source.id):
            flash('A source with this name already exists', 'danger')
            return redirect(url_for('accounts.account_plots', account_url=account_url))
        
        if source_id:
            # Update existing source
            source = Source.query.filter_by(id=source_id, account_id=account.id).first_or_404()
            source.name = name
            source.directory_filter = directory_filter
            source.include_subdirs = include_subdirs
            source.include_columns = include_columns
            source.datetime_column = datetime_column
            source.tail_only = tail_only
            source.data_points = data_points
            source.state = 'running'
            flash_message = 'Source updated successfully'
        else:
            # Create new source
            source = Source(
                name=name,
                directory_filter=directory_filter,
                include_subdirs=include_subdirs,
                include_columns=include_columns,
                datetime_column=datetime_column,
                tail_only=tail_only,
                data_points=data_points,
                account_id=account.id,
                state='running'
            )
            db.session.add(source)
            flash_message = 'Source created successfully'
        
        db.session.commit()
        
        # Get settings and initiate refresh
        success, error = initiate_source_refresh(account, source)
        if not success:
            flash(f'{flash_message} but refresh failed: {error}', 'warning')
        else:
            flash(flash_message, 'success')
            
        return redirect(url_for('accounts.account_plots', account_url=account_url))
        
    except Exception as e:
        db.session.rollback()
        flash('Error creating/updating source', 'danger')
        logging.error(f"Error creating/updating source: {e}")
        return redirect(url_for('accounts.account_plots', account_url=account_url))

@accounts_bp.route('/<account_url>/source/<int:source_id>/delete', methods=['POST'])
def delete_source(account_url, source_id):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        source = Source.query.filter_by(id=source_id, account_id=account.id).first_or_404()
        
        # Get all plot IDs from this source before deleting
        plot_ids = [str(plot.id) for plot in source.plots]
        
        # Clean up layout configurations for all plots in this source
        layouts = Layout.query.filter_by(account_id=account.id).all()
        for layout in layouts:
            if layout.config:
                try:
                    config = json.loads(layout.config)
                    if not isinstance(config, list):
                        logging.error(f"Invalid layout config format for layout {layout.id}: expected list")
                        continue
                        
                    # Remove any widgets that reference any plot from this source
                    config = [widget for widget in config if str(widget.get('plotId', '')) not in plot_ids]
                    layout.config = json.dumps(config)
                except json.JSONDecodeError as e:
                    logging.error(f"Invalid JSON in layout {layout.id} config: {e}")
                    continue  # Skip this layout instead of resetting it
        
        # Delete the source (this will cascade delete all its plots)
        db.session.delete(source)
        db.session.commit()
        
        flash('Source deleted successfully.', 'success')
    except Exception as e:
        db.session.rollback()
        flash('Error deleting source.', 'error')
        logging.error(f"Error deleting source: {e}")
    
    return redirect(url_for('accounts.account_plots', account_url=account_url))

@accounts_bp.route('/<account_url>/source/<int:source_id>/edit', methods=['POST'])
def edit_source(account_url, source_id):
    try:
        source = Source.query.get_or_404(source_id)
        
        # Get form data
        name = request.form.get('name', '').strip()
        directory_filter = request.form.get('directory_filter', '')
        include_subdirs = request.form.get('include_subdirs') == 'on'
        include_columns = request.form.get('include_columns', '')
        datetime_column = request.form.get('datetime_column', '')
        tail_only = request.form.get('tail_only') == 'on'
        data_points = request.form.get('data_points', type=int)
        
        # Update source
        source.name = name
        source.directory_filter = directory_filter
        source.include_subdirs = include_subdirs
        source.include_columns = include_columns
        source.datetime_column = datetime_column
        source.tail_only = tail_only
        source.data_points = data_points
        
        db.session.commit()

        # Get settings and initiate refresh
        settings = Setting.query.filter_by(account_id=source.account_id).first_or_404()
        success, error = (source, settings)
        if not success:
            flash(f'Source updated but refresh failed: {error}', 'warning')
        else:
            flash('Source updated successfully', 'success')
            
        return redirect(url_for('accounts.account_plots', account_url=account_url))
        
    except Exception as e:
        db.session.rollback()
        flash('Error updating source', 'error')
        logging.error(f"Error updating source: {e}")
        return redirect(url_for('accounts.account_plots', account_url=account_url))

@accounts_bp.route('/<account_url>/source/<int:source_id>/plot', methods=['POST'])
def create_plot(account_url, source_id):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        
        source = Source.query.filter_by(id=source_id, account_id=account.id).first_or_404()
        
        plot_name = request.form.get('name')
        plot_type = request.form.get('type')

        # Validate plot name
        if not plot_name:
            flash('Plot name is required.', 'error')
            return redirect(url_for('accounts.account_plots', account_url=account_url))
        
        # Build config based on plot type
        config = {}
        if plot_type == 'timeline':
            y_data = request.form.get('y_data')
            
            # Validate datetime column
            if not source.datetime_column:
                print("[DEBUG] Error: Source has no datetime column")
                flash('Timeline plots require a datetime column in the source.', 'error')
                return redirect(url_for('accounts.account_plots', account_url=account_url))
            
            if not y_data:
                print("[DEBUG] Error: Missing y_data for timeline plot")
                flash('Data Column is required for timeline plots.', 'error')
                return redirect(url_for('accounts.account_plots', account_url=account_url))
                
            config = {
                'x_data': source.datetime_column,  # Use source's datetime column directly
                'y_data': y_data
            }
        elif plot_type in ['box', 'bar', 'table']:
            y_data = request.form.get('y_data')
            print(f"[DEBUG] {plot_type} plot data - y_data: {y_data}")
            
            if not y_data:
                print("[DEBUG] Error: Missing y_data for plot")
                flash('Data column is required.', 'error')
                return redirect(url_for('accounts.account_plots', account_url=account_url))
                
            config = {
                'y_data': y_data
            }
        
        # Collect advanced options
        advanced_options = []
        if request.form.get('accumulate') == 'on':
            advanced_options.append('accumulate')
        if request.form.get('last_value') == 'on':
            advanced_options.append('last_value')
        
        # Create new plot with JSON config
        plot = Plot(
            source_id=source_id,
            name=plot_name,
            type=plot_type,
            config=json.dumps(config),
            group_by=request.form.get('group_by', type=int, default=None),  # Handle group_by field
            advanced=json.dumps(advanced_options)  # Add advanced options
        )
        
        # Add to session to set up relationships
        db.session.add(plot)
        db.session.flush()  # This sets up relationships without committing
        
        # Process plot data and save it to plot.data
        plot_data = get_plot_data(plot, source, account)
        
        if not plot_data:
            db.session.rollback()
            print("[DEBUG] Error: No plot data generated")
            flash('Error: No data available for the selected columns.', 'error')
            return redirect(url_for('accounts.account_plots', account_url=account_url))
        
        plot.data = json.dumps(plot_data)
        db.session.commit()
        
        return redirect(url_for('accounts.account_plots', account_url=account_url))
    except Exception as e:
        db.session.rollback()
        print(f"[DEBUG] Error creating plot: {str(e)}")
        print(f"[DEBUG] Traceback: {traceback.format_exc()}")
        logging.error(f"Error creating plot: {e}")
        flash('Error creating plot.', 'error')
    
    return redirect(url_for('accounts.account_plots', account_url=account_url))

@accounts_bp.route('/<account_url>/plot/<int:plot_id>/delete', methods=['POST'])
def delete_plot(account_url, plot_id):
    account = Account.query.filter_by(url=account_url).first_or_404()
    plot = Plot.query.get_or_404(plot_id)
    
    # Ensure plot belongs to account
    if plot.source.account_id != account.id:
        abort(403)
    
    # Clean up layout configurations
    layouts = Layout.query.filter_by(account_id=account.id).all()
    for layout in layouts:
        if layout.config:
            try:
                config = json.loads(layout.config)
                if not isinstance(config, list):
                    logging.error(f"Invalid layout config format for layout {layout.id}: expected list")
                    continue
                    
                # Remove any widgets that reference this plot
                config = [widget for widget in config if str(widget.get('plotId', '')) != str(plot_id)]
                layout.config = json.dumps(config)
            except json.JSONDecodeError as e:
                logging.error(f"Invalid JSON in layout {layout.id} config: {e}")
                continue  # Skip this layout instead of resetting it
    
    # Delete the plot
    db.session.delete(plot)
    
    try:
        db.session.commit()
        flash('Plot deleted successfully', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting plot: {str(e)}', 'danger')
    
    return redirect(url_for('accounts.account_plots', account_url=account_url))

@accounts_bp.route('/<account_url>/layout/<int:layout_id>', methods=['POST'])
def update_layout(account_url, layout_id):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        layout = Layout.query.filter_by(id=layout_id, account_id=account.id).first_or_404()
        
        data = request.get_json()
        
        # Validate layout name
        name = data.get('name', '').strip()
        if not name:
            return jsonify({
                'success': False,
                'error': 'Layout name cannot be empty'
            }), 400
            
        layout.name = name
        layout.config = json.dumps(data['config'])
        layout.time_range = data['time_range']
        
        db.session.commit()
        
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error updating layout {layout_id}: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

def _get_layout_data(account, layout):
    """Helper function to get layout data and plots consistently."""
    try:
        # Parse layout config
        config = json.loads(layout.config)
        if not isinstance(config, list):
            logging.error(f"Invalid layout config format for layout {layout.id}: expected list, got {type(config)}")
            return None, "Invalid layout configuration format"
            
        # Validate each item in the config
        required_plot_ids = []
        for item in config:
            if not isinstance(item, dict):
                logging.error(f"Invalid layout config item: expected dict, got {type(item)}")
                return None, "Invalid layout configuration item format"
                
            if 'plotId' not in item:
                logging.error("Layout config item missing plotId")
                return None, "Layout configuration missing plot ID"
                
            try:
                plot_id = int(item['plotId'])
                required_plot_ids.append(plot_id)
                # Verify the plot exists and belongs to this account
                plot = Plot.query.join(Source).filter(
                    Plot.id == plot_id,
                    Source.account_id == account.id
                ).first()
                if not plot:
                    logging.error(f"Plot {plot_id} not found or does not belong to account")
                    return None, f"Plot {plot_id} not found"
            except (ValueError, TypeError):
                logging.error(f"Invalid plotId value: {item['plotId']}")
                return None, "Invalid plot ID in layout configuration"

        # Get all required plots
        plots = Plot.query.filter(Plot.id.in_(required_plot_ids)).all()
        
        # Get unique sources and pre-fetch their data
        source_data = {}
        unique_sources = {plot.source_id: plot.source for plot in plots}
        
        # Pre-fetch source data
        for source in unique_sources.values():
            start_time = time.time()
            csv_content = download_source_file(account.settings, source)
            download_time = time.time() - start_time
            logging.info(f"Source {source.name} download took {download_time:.2f} seconds")
            source_data[source.id] = csv_content

        # Generate plot information with source data
        plot_info_arr = []
        for plot in plots:
            start_time = time.time()
            plot_info = get_plot_info(plot, source_data.get(plot.source_id))
            plot_time = time.time() - start_time
            logging.info(f"Plot {plot.name} generation took {plot_time:.2f} seconds")
            if plot_info:
                plot_info_arr.append(plot_info)
        
        # Create a new layout object with parsed config
        layout_data = {
            'id': layout.id,
            'name': layout.name,
            'config': config,
            'is_default': layout.is_default,
            'show_nav': layout.show_nav,
            'time_range': layout.time_range
        }
        
        return layout_data, plot_info_arr
    except json.JSONDecodeError as e:
        logging.error(f"Error parsing layout config JSON for layout {layout.id}: {e}")
        return None, "Invalid layout configuration JSON"
    except Exception as e:
        logging.error(f"Error getting layout data: {e}")
        return None, str(e)

@accounts_bp.route('/<account_url>/layout/<int:layout_id>', methods=['GET'])
def layout_view(account_url, layout_id):
    logger.info(f"Starting layout_view for account {account_url}, layout {layout_id}")
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        
        # Get the layout
        layout = Layout.query.filter_by(id=layout_id, account_id=account.id).first_or_404()
        g.title = layout.name
        
        # Get layout data and plots using the helper function
        layout_data, plot_info_arr = _get_layout_data(account, layout)
        if not layout_data:
            flash("Error loading layout", "error")
            return redirect(url_for('accounts.account_plots', account_url=account_url))
        
        # Render the full layout page
        return render_template('layout.html',
                           account=account,
                           layout=layout_data,
                           plot_info_arr=plot_info_arr)
                
    except Exception as e:
        logging.error(f"Error loading layout view for {account_url}: {e}")
        traceback.print_exc()
        flash("Error loading layout view", "error")
        return redirect(url_for('accounts.account_plots', account_url=account_url))

@accounts_bp.route('/<account_url>/layout/<int:layout_id>/grid', methods=['GET'])
def layout_grid(account_url, layout_id):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        layout = Layout.query.filter_by(id=layout_id, account_id=account.id).first_or_404()
        
        # Get layout data and plots using the helper function
        result = _get_layout_data(account, layout)
        if not result[0]:  # If layout_data is None
            return f"Error loading layout grid: {result[1]}", 500
            
        layout_data, plot_info_arr = result
        
        return render_template('components/layout_grid.html',
                             account=account,
                             layout=layout_data,
                             plot_info_arr=plot_info_arr)
    except Exception as e:
        logging.error(f"Error loading layout grid for {account_url}: {e}")
        return "Error loading layout grid", 500

@accounts_bp.route('/<account_url>/layout/<int:layout_id>/edit', methods=['GET'])
def layout_edit(account_url, layout_id):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        
        layout = Layout.query.filter_by(id=layout_id, account_id=account.id).first_or_404()
        g.title = layout.name
        
        # Get all plots for this account
        plots = []
        for source in account.sources:
            for plot in source.plots:
                plots.append(plot)
                
        # If layout name matches a source name, only show plots from that source
        matching_source = next((source for source in account.sources if source.name == layout.name), None)
        if matching_source:
            plots = [plot for plot in plots if plot.source_id == matching_source.id]
        
        # Sort plots by source name and plot name
        plots.sort(key=lambda x: (x.source.name, x.name))
        
        return render_template('layout_edit.html', 
                             account=account, 
                             layout=layout, 
                             plots=plots)
                            
    except Exception as e:
        logging.error(f"Error loading layout edit for {account_url}: {e}")
        traceback.print_exc()
        return "There was an issue loading the layout edit page.", 500

@accounts_bp.route('/<account_url>/layout/<int:layout_id>/delete', methods=['POST'])
def delete_layout(account_url, layout_id):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        layout = Layout.query.filter_by(id=layout_id, account_id=account.id).first_or_404()
        
        was_default = layout.is_default
        db.session.delete(layout)
        
        # If we deleted the default layout and other layouts exist,
        # make the first remaining layout the default
        if was_default:
            remaining_layout = Layout.query.filter_by(account_id=account.id).first()
            if remaining_layout:
                remaining_layout.is_default = True
        
        db.session.commit()
        flash('Layout deleted successfully.', 'success')
    except Exception as e:
        logging.error(f"Error deleting layout {layout_id} for account {account_url}: {e}")
        flash('Error deleting layout.', 'error')
    
    return redirect(url_for('accounts.account_plots', account_url=account_url))

@accounts_bp.route('/<account_url>/layout', methods=['POST'])
def create_layout(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        layout_name = request.form.get('name')
        
        if not layout_name:
            flash('Layout name is required.', 'error')
            return redirect(url_for('accounts.account_plots', account_url=account_url))
        
        # Check if this is the first layout or if is_default was requested
        is_first_layout = len(account.layouts) == 0
        should_be_default = is_first_layout or request.form.get('is_default') == 'on'
        
        # If this layout will be default, set all other layouts to not default
        if should_be_default:
            Layout.query.filter_by(account_id=account.id).update({'is_default': False})
        
        # Create new layout with empty config and default time_range
        layout = Layout(
            account_id=account.id,
            name=layout_name,
            config=json.dumps([]),
            is_default=should_be_default,  # Set based on first layout or form input
            show_nav=request.form.get('show_nav') == 'on',
            time_range='all'  # Default time range
        )
        db.session.add(layout)
        db.session.commit()
        
        return redirect(url_for('accounts.layout_edit', account_url=account_url, layout_id=layout.id))
    except Exception as e:
        logging.error(f"Error creating layout for {account_url}: {e}")
        db.session.rollback()  # Add rollback on error
        flash('Error creating layout.', 'error')  # Add user feedback
        return redirect(url_for('accounts.account_plots', account_url=account_url))

@accounts_bp.route('/<account_url>/layout/<int:layout_id>/update', methods=['POST'])
def update_layout_settings(account_url, layout_id):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        layout = Layout.query.filter_by(id=layout_id, account_id=account.id).first_or_404()
        
        # Update layout name if provided
        if 'name' in request.form:
            layout.name = request.form['name']
        
        # Handle is_default update
        if 'is_default' in request.form:
            # Check if this is the only layout
            layout_count = Layout.query.filter_by(account_id=account.id).count()
            if layout_count > 1:
                # Set all other layouts to not default
                Layout.query.filter(Layout.account_id == account.id, Layout.id != layout_id).update({'is_default': False})
            layout.is_default = request.form.get('is_default') == 'on'
        
        # Handle show_nav update
        layout.show_nav = request.form.get('show_nav') == 'on'
        
        # Optionally update config if present
        if 'config' in request.form:
            layout.config = request.form['config']
        
        db.session.commit()
        flash('Layout settings updated successfully.', 'success')
    except Exception as e:
        logging.error(f"Error updating layout settings for {layout_id}: {e}")
        flash('Error updating layout settings.', 'error')
    
    return redirect(url_for('accounts.account_plots', account_url=account_url))

@accounts_bp.route('/<account_url>/file/<int:file_id>/header', methods=['GET'])
def get_file_header(account_url, file_id):
    try:
        # Get account by URL
        account = Account.query.filter_by(url=account_url).first_or_404()
        file = File.query.filter_by(id=file_id, account_id=account.id).first_or_404()

        # Create temporary source object to use existing function
        temp_source = Source(file_id=file.id)
        
        # Get account settings
        settings = Setting.query.filter_by(account_id=account.id).first_or_404()

        # Get header and first row
        result = get_source_file_header(settings, temp_source)
        if result['error']:
            return jsonify({'error': result['error']}), 400

        # Parse header into columns
        columns = [col.strip() for col in result['header'].split(',') if col.strip()]  # Skip empty columns
        # Check first row for datetime values
        datetime_columns = []
        if result['first_row']:
            row_values = result['first_row'].split(',')
            for i, value in enumerate(row_values):
                if i < len(columns):  # Ensure we don't go out of bounds
                    try:
                        value = value.strip()
                        if value:  # Skip empty values
                            # Try to parse as datetime with more strict validation
                            parsed_date = parser.parse(value)
                            # Only consider it a datetime if it has both date and time components
                            if (parsed_date.hour != 0 or parsed_date.minute != 0 or 
                                parsed_date.second != 0 or parsed_date.microsecond != 0):
                                datetime_columns.append(columns[i])
                    except (ValueError, TypeError):
                        continue

        return jsonify({
            'success': True,
            'header': result['header'],
            'columns': columns,
            'datetime_columns': datetime_columns
        })

    except Exception as e:
        logging.error(f"Error getting file header: {e}")
        return jsonify({'error': str(e)}), 500

@accounts_bp.route('/<account_url>/download_files', methods=['POST'])
def download_files(account_url):
    account = Account.query.filter_by(url=account_url).first_or_404()
    account_settings = Setting.query.filter_by(account_id=account.id).first_or_404()
    
    data = request.get_json()
    file_ids = data.get('file_ids', [])
    directory = data.get('directory')
    
    # If directory is provided, get all file IDs in that directory
    if directory:
        files = File.query.filter_by(account_id=account.id)\
            .filter(File.key.like(f"{directory}/%"))\
            .filter(~File.key.like('.%'))\
            .filter(~File.key.contains('/.'))\
            .all()
        file_ids = [f.id for f in files]
    
    if not file_ids:
        return jsonify({'error': 'No files selected'}), 400
    
    # Get all selected files
    files = File.query.filter(File.id.in_(file_ids)).all()
    if not files:
        return jsonify({'error': 'No files found'}), 404
    
    # For single file, download directly
    if len(files) == 1:
        content = download_s3_file(account_settings, files[0])
        if content is None:  # Only skip if download failed (None), not if file is empty
            return jsonify({'error': 'Error downloading file'}), 500
        
        filename = files[0].key.split('/')[-1]
        return send_file(
            io.BytesIO(content),
            mimetype='application/octet-stream',
            as_attachment=True,
            download_name=filename
        )
    
    # For multiple files, create a zip
    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp()
        zip_path = os.path.join(temp_dir, 'files.zip')
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file in files:
                content = download_s3_file(account_settings, file)
                if content is None:  # Only skip if download failed (None), not if file is empty
                    print(f"Failed to download {file.key}")
                    continue
                
                # Use the full directory structure from the key
                zipf.writestr(file.key, content)
        
        # Generate timestamp for unique filename
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        download_name = f'hublink_{timestamp}.zip'
        
        return send_file(
            zip_path,
            mimetype='application/zip',
            as_attachment=True,
            download_name=download_name
        )
    
    except Exception as e:
        print(f"Error creating zip file: {str(e)}")
        return jsonify({'error': 'Failed to create zip file'}), 500
    
    finally:
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

@accounts_bp.route('/<account_url>/files/delete', methods=['POST'])
@admin_required
def delete_files(account_url):
    try:
        # Get target account and its settings
        target_account = Account.query.filter_by(url=account_url).first_or_404()
        target_settings = Setting.query.filter_by(account_id=target_account.id).first_or_404()
        
        # Get admin account settings for deletion
        admin_account = Account.query.filter_by(is_admin=True).first()
        if not admin_account:
            flash('Admin account not found', 'error')
            return jsonify({'error': 'Admin account not found'}), 500
            
        admin_settings = Setting.query.filter_by(account_id=admin_account.id).first()
        if not admin_settings:
            flash('Admin settings not found', 'error')
            return jsonify({'error': 'Admin settings not found'}), 500
        
        data = request.get_json()
        file_ids = data.get('file_ids', [])
        current_directory = data.get('directory')
        
        if not file_ids:
            flash('No files selected for deletion', 'error')
            return jsonify({'error': 'No files selected for deletion'}), 400
            
        # Get all selected files from target account
        files = File.query.filter(File.id.in_(file_ids), File.account_id == target_account.id).all()
        if not files:
            flash('No files found', 'error')
            return jsonify({'error': 'No files found'}), 404
            
        # Delete files using admin settings
        success, error_message = delete_files_from_s3(admin_settings, files)
        
        if not success:
            flash(error_message or 'Failed to delete files', 'error')
            return jsonify({'error': error_message or 'Failed to delete files'}), 500
            
        # Rebuild S3 files using target account settings
        rebuild_S3_files(target_settings)
        
        # Add success flash message
        flash(f'Successfully deleted {len(files)} file(s)', 'success')
        
        # If we were in a directory view, check if it still has files
        redirect_url = request.referrer
        if current_directory:
            # Check if directory still has files
            has_files = File.query.filter_by(account_id=target_account.id)\
                .filter(File.key.like(f"{current_directory}/%"))\
                .first() is not None
            
            if not has_files:
                # Set redirect to base data page if no files left in directory
                redirect_url = url_for('accounts.account_data', account_url=account_url)
        
        # Return success with redirect URL
        return jsonify({
            'success': True,
            'redirect': redirect_url or url_for('accounts.account_data', account_url=account_url)
        })
            
    except Exception as e:
        logging.error(f"Error deleting files for account {account_url}: {e}")
        flash('An error occurred while deleting files', 'error')
        return jsonify({'error': str(e)}), 500

@accounts_bp.route('/<account_url>/data/content', methods=['GET'])
def account_data_content(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        g.account = account  # Make account available in global context
        
        # Get directory filter
        directory = request.args.get('directory')
        
        # Get page number
        page = request.args.get('page', 1, type=int)
        per_page = 50
        
        # Combine base filters into a single expression
        base_filters = and_(
            File.account_id == account.id,
            not_(File.key.like('.%')),
            not_(File.key.contains('/.')),
            not_(File.key.like('__MACOSX%'))
        )
        
        # Initialize files_query with base filters
        files_query = File.query.filter(base_filters)
        
        # Add directory-specific filters if needed
        if directory:
            if directory == '/':
                # For root directory, add filter for files without slashes
                files_query = files_query.filter(not_(File.key.contains('/')))
            else:
                # Remove leading slash if present for consistency
                directory = directory.lstrip('/')
                # Match files that are directly in this directory
                files_query = files_query.filter(
                    and_(
                        File.key.like(f"{directory}/%"),
                        not_(File.key.like(f"{directory}/%/%"))
                    )
                )

        # Order by last modified and paginate
        pagination = files_query.order_by(File.last_modified.desc()).paginate(
            page=page, 
            per_page=per_page,
            error_out=False
        )
        
        # Get total count of files in current view
        total_files = files_query.count()
        
        # Get directories for dropdown
        directories = get_directory_paths(account.id)
        
        # Ensure datetime objects have timezone info for comparison
        now = datetime.now(timezone.utc)
        files = []
        for file in pagination.items:
            # Ensure last_checked has timezone info for comparison
            if file.last_modified and file.last_modified.tzinfo is None:
                file.last_modified = file.last_modified.replace(tzinfo=timezone.utc)
            files.append(file)
        
        return render_template('components/data_content.html',
                             account=account,
                             recent_files=files,
                             pagination=pagination,
                             now=now,
                             directory=directory,
                             current_directory=directory,
                             directories=directories,
                             total_files=total_files)
                             
    except Exception as e:
        logger.error(f"Error loading data content: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return f"Error loading data content: {str(e)}", 500
    
@accounts_bp.route('/<account_url>/gateways', methods=['GET'])
def dashboard_gateways(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        analytics = get_analytics(account.id)  # Get analytics for gateway counts
        if not analytics:
            return "Error loading analytics", 500
            
        # Get recent gateway activity
        gateways = Gateway.query.filter_by(account_id=account.id)\
            .order_by(Gateway.created_at.desc())\
            .limit(100)\
            .all()
            
        return render_template('components/dashboard_gateways.html',
                             account=account,
                             gateways=gateways,
                             analytics=analytics)
    except Exception as e:
        logging.error(f"Error loading dashboard gateways for {account_url}: {e}")
        return "Error loading gateway activity", 500
    
@accounts_bp.route('/<account_url>/dashboard/stats', methods=['GET'])
def dashboard_stats(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        analytics = get_analytics(account.id)  # Pass account.id to get_analytics
        if not analytics:
            return "Error loading analytics", 500
        return render_template('components/dashboard_stats.html', 
                             account=account, 
                             analytics=analytics)
    except Exception as e:
        logging.error(f"Error loading dashboard stats for {account_url}: {e}")
        return "Error loading dashboard stats", 500
    
@accounts_bp.route('/<account_url>/dashboard/uploads', methods=['GET'])
def dashboard_uploads(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        
        # Get account's timezone
        account_tz = account.settings.timezone
        
        # Calculate date range in account's timezone
        end_date = datetime.now(pytz.timezone(account_tz))
        start_date = end_date - timedelta(days=14)  # 15 days ago
        
        # Create a list of all dates in range (including today)
        date_range = [(start_date + timedelta(days=x)).date() for x in range(15)]
        
        # Get files modified in the last 14 days
        # Ensure we get all of today's files by extending to end of current day
        end_of_today = end_date.replace(hour=23, minute=59, second=59, microsecond=999999)
        
        # Convert timestamps to UTC for database query
        start_date_utc = start_date.astimezone(timezone.utc)
        end_date_utc = end_of_today.astimezone(timezone.utc)
        
        files = File.query.filter_by(account_id=account.id)\
            .filter(~File.key.like('.%'))\
            .filter(~File.key.contains('/.')) \
            .filter(File.last_modified >= start_date_utc) \
            .filter(File.last_modified <= end_date_utc) \
            .order_by(File.last_modified.desc()).all()
        
        # Initialize counts for all days using string dates as keys
        daily_counts = {date.strftime('%Y-%m-%d'): 0 for date in date_range}
        
        # Count files per day in account's timezone
        for file in files:
            file_date = file.last_modified.astimezone(pytz.timezone(account_tz)).date()
            date_str = file_date.strftime('%Y-%m-%d')
            if date_str in daily_counts:
                daily_counts[date_str] += 1
        
        # Format dates for the template
        file_uploads = [date.strftime('%Y-%m-%d') for date in date_range]
        
        return render_template('components/dashboard_uploads.html',
                             account=account,
                             file_uploads=file_uploads,
                             daily_counts=daily_counts)
                             
    except Exception as e:
        logging.error(f"Error loading dashboard uploads for {account_url}: {e}")
        return "Error loading uploads chart", 500
    
@accounts_bp.route('/<account_url>/dashboard/dirs', methods=['GET'])
def dashboard_dirs(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        
        # Get all files for this account
        files = File.query.filter_by(account_id=account.id)\
            .filter(~File.key.like('.%'))\
            .filter(~File.key.contains('/.')) \
            .order_by(File.last_modified.desc()) \
            .all()
        
        # Process directory data with timestamps
        dir_data = {}
        for file in files:
            path_parts = file.key.split('/')
            if len(path_parts) > 1:  # If file is in a directory
                dir_path = '/'.join(path_parts[:-1])
                if not any(part.startswith('.') for part in path_parts):
                    if dir_path not in dir_data:
                        dir_data[dir_path] = {
                            'count': 0,
                            'size': 0,
                            'latest_date': None,
                            'latest_file': None
                        }
                    dir_data[dir_path]['count'] += 1
                    dir_data[dir_path]['size'] += file.size
                    
                    # Update latest date if this file is newer
                    if not dir_data[dir_path]['latest_date'] or \
                       (file.last_modified and file.last_modified > dir_data[dir_path]['latest_date']):
                        dir_data[dir_path]['latest_date'] = file.last_modified
                        dir_data[dir_path]['latest_file'] = file.key.split('/')[-1]
        
        # Sort directories by latest_date and take top 10
        sorted_dirs = sorted(
            [(dir_name, data) for dir_name, data in dir_data.items()],
            key=lambda x: x[1]['latest_date'] or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True
        )[:10]
        
        # Prepare data for template
        dir_names = [d[0] for d in sorted_dirs]
        dir_counts = [d[1]['count'] for d in sorted_dirs]
        dir_details = {
            d[0]: {
                'size': d[1]['size'],
                'latest_date': format_datetime(d[1]['latest_date'], account.settings.timezone, 'absolute') if d[1]['latest_date'] else None,
                'latest_file': d[1]['latest_file']
            } for d in sorted_dirs
        }
        
        return render_template('components/dashboard_dirs.html',
                             account=account,
                             dir_names=dir_names,
                             dir_counts=dir_counts,
                             dir_details=dir_details)
    except Exception as e:
        logging.error(f"Error loading dashboard directories for {account_url}: {e}")
        return "Error loading directory chart", 500

# Route to get source list for HTMX updates
@accounts_bp.route('/<account_url>/source/list', methods=['GET'])
def source_list(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        sources = Source.query.filter_by(account_id=account.id).all()
        
        return render_template('components/source_list.html',
                           account=account,
                           sources=sources)
    except Exception as e:
        logging.error(f"Error loading source list for {account_url}: {e}")
        return "Error loading source list", 500

@accounts_bp.route('/<account_url>/source/<int:source_id>/plot/<int:plot_id>/edit', methods=['POST'])
def edit_plot(account_url, source_id, plot_id):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        plot = Plot.query.filter_by(id=plot_id, source_id=source_id).first_or_404()
        
        # Update plot fields
        plot.name = request.form.get('name')
        plot.type = request.form.get('type')
        plot.group_by = request.form.get('group_by', type=int, default=None)  # Handle group_by field
        
        # Update config based on plot type
        config = {}
        if plot.type == 'timeline':
            y_data = request.form.get('y_data')
            if not y_data:
                flash('Data Column is required for timeline plots.', 'error')
                return redirect(url_for('accounts.account_plots', account_url=account_url))
                
            config = {
                'x_data': plot.source.datetime_column,
                'y_data': y_data
            }
        elif plot.type in ['box', 'bar', 'table']:
            y_data = request.form.get('y_data')
            if not y_data:
                flash('Data column is required.', 'error')
                return redirect(url_for('accounts.account_plots', account_url=account_url))
                
            config = {
                'y_data': y_data
            }
            
        plot.config = json.dumps(config)
        
        # Update advanced options
        advanced_options = []
        if request.form.get('accumulate') == 'on':
            advanced_options.append('accumulate')
        if request.form.get('last_value') == 'on':
            advanced_options.append('last_value')
        plot.advanced = json.dumps(advanced_options)
        
        db.session.commit()
        flash('Plot updated successfully', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash('Error updating plot', 'error')
        logging.error(f"Error updating plot: {e}")
    
    return redirect(url_for('accounts.account_plots', account_url=account_url))

@accounts_bp.route('/<account_url>/source/<int:source_id>/callback', methods=['POST'])
def source_callback(account_url, source_id):
    """Endpoint for Lambda function to update source status and file information."""
    try:
        data = request.get_json()
        if not data or 'key' not in data or 'size' not in data:
            return jsonify({
                'error': 'Missing required fields (key or size)',
                'status': 400
            }), 400
        
        # Get account and source directly using URL and ID
        account = Account.query.filter_by(url=account_url).first_or_404()
        source = Source.query.filter_by(id=source_id, account_id=account.id).first_or_404()
        
        logging.info(f"Processing Lambda callback for source: {source.name} (ID: {source.id})")
        
        # Update source fields
        is_success = not data.get('error')  # Success if no error field or error is empty
        source.state = 'success' if is_success else 'error'
        source.error = data.get('error')  # Store error message if present
        source.last_updated = datetime.now(timezone.utc)
        
        # Get matching files and calculate max_path_level
        matching_files = list_source_files(account, source)
        max_level = 0
        for file in matching_files:
            # Split path and count segments (including root)
            path_segments = file.key.strip('/').split('/')
            max_level = max(max_level, len(path_segments))
        
        source.max_path_level = max_level
        
        # Handle file record
        file = File.query.filter_by(account_id=account.id, key=data['key']).first()
        if not file:
            logging.info(f"Creating new file record for key: {data['key']}")
            file = File(
                account_id=account.id,
                key=data['key'],
                url=generate_s3_url(account.settings.bucket_name, data['key']),
                size=data['size'],
                last_modified=datetime.now(timezone.utc),
                version=1
            )
            db.session.add(file)
            db.session.flush()
        else:
            file.size = data['size']
            file.version += 1
            file.last_modified = datetime.now(timezone.utc)
        
        source.file_id = file.id
        
        db.session.commit()
        
        return jsonify({
            'message': 'Source updated successfully',
            'status': 200
        })
        
    except Exception as e:
        logging.error(f"Error in source_callback: {e}")
        db.session.rollback()
        return jsonify({
            'error': 'Internal server error',
            'status': 500
        }), 500

"""
Gateway POST endpoint documentation:

Fields:
    name: string (required) - Name of the gateway
    nodes: array of strings (optional) - Array of node UUIDs associated with this gateway

Example request body:
    {
        "name": "Gateway1",
        "nodes": ["550e8400-e29b-41d4-a716-446655440000", "550e8400-e29b-41d4-a716-446655440001"]
    }

Example curl:
    curl -X POST http://127.0.0.1:5000/<account_url>/gateway \
        -H "Content-Type: application/json" \
        -d '{"name": "Gateway1", "nodes": ["550e8400-e29b-41d4-a716-446655440000"]}'

Response:
    {
        "message": "Gateway and nodes updated successfully",
        "gateway_id": 1,
        "node_count": 1
    }
"""

# Route to create gateway and nodes
@accounts_bp.route('/<account_url>/gateway', methods=['POST'])
def create_gateway(account_url):
    try:
        # Get account and increment ping counter
        account = Account.query.filter_by(url=account_url).first_or_404()
        account.count_gateway_pings += 1
        
        # Parse request data
        data = request.get_json()
        if not data or 'name' not in data:
            return jsonify({'error': 'Gateway name is required'}), 400
            
        gateway_name = data.get('name')
        node_uuids = data.get('nodes', [])
        
        # Extract client IP address
        ip_address = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
        
        # Create new gateway
        gateway = Gateway(
            account_id=account.id,
            name=gateway_name,
            ip_address=ip_address,
            created_at=datetime.now(timezone.utc)
        )
        db.session.add(gateway)
        
        # Commit to get gateway ID
        db.session.commit()
        
        # Create new node records for each UUID
        for uuid in node_uuids:
            node = Node(
                gateway_id=gateway.id,
                uuid=uuid,
                created_at=datetime.now(timezone.utc)
            )
            db.session.add(node)
        
        # Final commit for all changes
        db.session.commit()
        
        return jsonify({
            'message': 'Gateway and nodes created successfully',
            'gateway_id': gateway.id,
            'node_count': len(node_uuids)
        }), 200
        
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error creating gateway for {account_url}: {e}")
        return jsonify({'error': str(e)}), 500

@accounts_bp.route('/<account_url>/files.json', methods=['GET'])
@accounts_bp.route('/<account_url>/files.json/<since_datetime>', methods=['GET'])
def list_files_json(account_url, since_datetime=None):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        
        # Start with base query including non-hidden file filter
        query = File.query.filter_by(account_id=account.id)\
            .filter(~File.key.like('.%'))\
            .filter(~File.key.contains('/.'))\
            .filter(~File.key.like('__MACOSX%'))
            
        # Add datetime filter if provided
        if since_datetime:
            try:
                filter_date = datetime.fromisoformat(since_datetime.replace('Z', '+00:00'))
                query = query.filter(File.last_modified > filter_date)
            except ValueError as e:
                return jsonify({
                    'error': f'Invalid datetime format: {str(e)}'
                }), 400
        
        # Get all matching files
        files = query.all()
        
        # Format response
        response = [{
            'key': file.key,
            'size': file.size
        } for file in files]
        
        return jsonify(response)
        
    except Exception as e:
        logging.error(f"Error listing files for {account_url}: {e}")
        return jsonify({
            'error': str(e)
        }), 500

"""
Example curl command to test this endpoint:

curl -X POST http://127.0.0.1:5000/your-account-url/files \
  -H "Content-Type: application/json" \
  -d '{
    "uploaded_files": [
      "data/file1.csv",
      "data/file2.csv",
      "experiments/test.csv"
    ]
  }'

Response on success:
{
    "success": true,
    "message": "Updated 3 files, marked 2 sources for refresh",
    "updated_files": 3,
    "marked_sources": 2
}

Response on error:
{
    "error": "Error message here"
}
"""
@accounts_bp.route('/<account_url>/files', methods=['POST'])
def update_files(account_url):
    """Update database entries for specific S3 files.
    
    Expected JSON payload:
    {
        "uploaded_files": ["file1.csv", "path/to/file2.csv", ...]
    }
    """
    try:
        # Get account and settings
        account = Account.query.filter_by(url=account_url).first_or_404()
        settings = Setting.query.filter_by(account_id=account.id).first_or_404()
        
        # Get file keys from request
        data = request.get_json()
        if not data or 'uploaded_files' not in data:
            return jsonify({
                'error': 'Missing uploaded_files in request body'
            }), 400
            
        file_keys = data['uploaded_files']
        if not file_keys:
            return jsonify({
                'error': 'No files provided'
            }), 400
            
        # Update files in database
        affected_files = update_specific_files(settings, file_keys)
        
        # Update sources using helper function
        refresh_count, _ = _update_sources_for_files(account, affected_files)
        
        if affected_files:
            return jsonify({
                'message': 'Files updated successfully',
                'affected_files': len(affected_files),
                'marked_for_refresh': refresh_count
            }), 200
        else:
            return jsonify({
                'message': 'No files were affected',
                'affected_files': 0,
                'marked_for_refresh': 0
            }), 200
            
    except Exception as e:
        logging.error(f"Error updating files for {account_url}: {e}")
        return jsonify({
            'error': str(e)
        }), 500