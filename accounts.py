from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, g, send_file, current_app as app, session
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
from sqlalchemy import func, cast, Date
from sqlalchemy.dialects.postgresql import INTERVAL
import random

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

        print(f"aws_settings_changed: {aws_settings_changed}")
        print(f"original_access_key: {original_access_key}")
        print(f"settings.aws_access_key_id: {settings.aws_access_key_id}")
        print(f"original_secret_key: {original_secret_key}")
        print(f"settings.aws_secret_access_key: {settings.aws_secret_access_key}")
        print(f"original_bucket_name: {original_bucket_name}")
        print(f"settings.bucket_name: {settings.bucket_name}")

        # we would need to rebuild if AWS settings changed

        db.session.commit()

        if aws_settings_changed and g.user and g.user.is_admin:
            flash("AWS settings updated successfully!", "success")
        else:
            flash("Settings updated successfully!", "success")

        return redirect(url_for('accounts.account_settings', account_url=account_url))

    except Exception as e:
        db.session.rollback()
        logging.error(f"Error updating settings for {account_url}: {e}")
        flash("An error occurred while updating settings", "error")
        return redirect(url_for('accounts.account_settings', account_url=account_url))

def get_directory_paths(account_id, include_all_subpaths=False):
    """Get unique directory paths for an account.
    
    Args:
        account_id: The ID of the account to get paths for
        include_all_subpaths: If True, include all intermediate paths. If False, only include
                             the deepest directory that directly contains files.
    """
    files = File.query.filter_by(account_id=account_id)\
        .filter(~File.key.like('.%'))\
        .filter(~File.key.contains('/.')) \
        .all()
    directories = {}
    
    for file in files:
        # Get the directory path by removing the file name
        path_parts = file.key.split('/')
        if len(path_parts) > 1:  # If file is in a directory
            if include_all_subpaths:
                # Add all intermediate paths
                for i in range(1, len(path_parts)):
                    dir_path = '/'.join(path_parts[:i])
                    if not any(part.startswith('.') for part in path_parts[:i]):
                        if dir_path not in directories:
                            directories[dir_path] = {'total_files': 0, 'total_archived': 0}
                        directories[dir_path]['total_files'] += 1
                        if file.archived:
                            directories[dir_path]['total_archived'] += 1
            else:
                # Just add the immediate parent directory (original behavior)
                dir_path = '/'.join(path_parts[:-1])
                if not any(part.startswith('.') for part in path_parts):
                    if dir_path not in directories:
                        directories[dir_path] = {'total_files': 0, 'total_archived': 0}
                    directories[dir_path]['total_files'] += 1
                    if file.archived:
                        directories[dir_path]['total_archived'] += 1
    
    # Convert to sorted list of dictionaries
    return sorted([{'path': path, **data} for path, data in directories.items()], key=lambda x: x['path'])

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
            
        # Get matching files for this source, excluding archived files
        matching_files = list_source_files(account, source)
        matching_files = [f for f in matching_files if not f.archived]
        
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
                config = layout.config_json  # Use config_json property
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
        directories = get_directory_paths(account.id, include_all_subpaths=True)

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
                    config = layout.config_json  # Use config_json property
                    if not isinstance(config, list):
                        logging.error(f"Invalid layout config format for layout {layout.id}: expected list")
                        continue
                        
                    # Remove any widgets that reference any plot from this source
                    config = [widget for widget in config if str(widget.get('plotId', '')) not in plot_ids]
                    layout.config_json = config  # Use config_json property
                except Exception as e:
                    logging.error(f"Error updating layout {layout.id} config: {e}")
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
        if plot_type in ['timeline', 'timebin']:  # Handle both timeline and timebin
            y_data = request.form.get('y_data')
            
            # Validate datetime column
            if not source.datetime_column:
                print("[DEBUG] Error: Source has no datetime column")
                flash('Timeline and Time Bin plots require a datetime column in the source.', 'error')
                return redirect(url_for('accounts.account_plots', account_url=account_url))
            
            if not y_data:
                print("[DEBUG] Error: Missing y_data for plot")
                flash('Data Column is required for timeline plots.', 'error')
                return redirect(url_for('accounts.account_plots', account_url=account_url))
                
            config = {
                'x_data': source.datetime_column,  # Use source's datetime column directly
                'y_data': y_data
            }
            
            # Add timebin specific config
            if plot_type == 'timebin':
                bin_hrs = request.form.get('bin_hrs', type=int, default=24)
                mean_nsum = request.form.get('mean_nsum') == 'on'  # True if checked
                
                # Validate bin_hrs
                if not bin_hrs or bin_hrs < 1 or bin_hrs > 24:
                    flash('Bin hours must be between 1 and 24.', 'error')
                    return redirect(url_for('accounts.account_plots', account_url=account_url))
                    
                config.update({
                    'bin_hrs': bin_hrs,
                    'mean_nsum': mean_nsum
                })
                
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
        
        # Create new plot without manually serializing config and advanced_options
        plot = Plot(
            source_id=source_id,
            name=plot_name,
            type=plot_type,
            group_by=request.form.get('group_by', type=int, default=None)
        )
        # Set JSON fields using the new properties
        plot.config_json = config
        plot.advanced_json = advanced_options
        
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
        if layout.config_json:  # Use config_json property
            try:
                # Config is already a list from JSONB, no need for json.loads
                config = layout.config_json
                if not isinstance(config, list):
                    logging.error(f"Invalid layout config format for layout {layout.id}: expected list")
                    continue
                    
                # Remove any widgets that reference this plot
                config = [widget for widget in config if str(widget.get('plotId', '')) != str(plot_id)]
                layout.config_json = config  # Use config_json property
            except Exception as e:
                logging.error(f"Error updating layout {layout.id} config: {e}")
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
        layout.config_json = data['config']  # Use the new property
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
        # Get config using the new property
        config = layout.config_json
        
        # Ensure config is a list
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
            'config': config,  # Already using parsed config from config_json
            'is_default': layout.is_default,
            'show_nav': layout.show_nav,
            'time_range': layout.time_range
        }
        
        return layout_data, plot_info_arr
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
            flash("Error loading layout", "danger")
            return redirect(url_for('accounts.account_plots', account_url=account_url))
        
        # Render the full layout page
        return render_template('layout.html',
                           account=account,
                           layout=layout_data,
                           plot_info_arr=plot_info_arr)
                
    except Exception as e:
        logging.error(f"Error loading layout view for {account_url}: {e}")
        traceback.print_exc()
        flash("Error loading layout view", "danger")
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
            is_default=should_be_default,  # Set based on first layout or form input
            show_nav=request.form.get('show_nav') == 'on',
            time_range='all'  # Default time range
        )
        # Set empty config using the new property
        layout.config_json = []
        
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
            layout.config_json = request.form['config']
        
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
    time_filter = data.get('time_filter')  # '24h' or '7d'
    custom_path = data.get('custom_path')  # New parameter for custom path downloads
    
    # Handle time-based filtering
    if time_filter:
        cutoff_time = datetime.now(timezone.utc)
        if time_filter == '24h':
            cutoff_time = cutoff_time - timedelta(hours=24)
        elif time_filter == '7d':
            cutoff_time = cutoff_time - timedelta(days=7)
            
        # Query files based on time filter
        query = File.query.filter_by(account_id=account.id)\
            .filter(File.last_modified >= cutoff_time)\
            .filter(~File.key.like('.%'))\
            .filter(~File.key.contains('/.'))
            
        # Add directory filter if specified
        if directory:
            query = query.filter(File.key.like(f"{directory}/%"))
            
        files = query.all()
        if not files:
            return jsonify({'error': 'No files found in the selected time range'}), 404
    elif custom_path:
        # Handle custom path downloads
        if custom_path:
            # Get files in this path and subpaths, excluding archived files
            files = File.query.filter_by(account_id=account.id)\
                .filter(File.key.like(f"{custom_path}/%"))\
                .filter(~File.key.like('.%'))\
                .filter(~File.key.contains('/.'))\
                .filter(File.archived == False)\
                .all()
        else:
            # Root level files
            files = File.query.filter_by(account_id=account.id)\
                .filter(~File.key.like('.%'))\
                .filter(~File.key.contains('/.'))\
                .filter(~File.key.contains('/'))\
                .filter(File.archived == False)\
                .all()
        
        if not files:
            return jsonify({'error': 'No files found in the selected path'}), 404
    else:
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

@accounts_bp.route('/<account_url>/files/archive', methods=['POST'])
def archive_files(account_url):
    app.logger.info(f"Archiving files for account {account_url}")
    try:
        # Get target account
        target_account = Account.query.filter_by(url=account_url).first_or_404()
        
        data = request.get_json()
        if not data or 'file_ids' not in data:
            return jsonify({'error': 'No file IDs provided'}), 400
            
        file_ids = data['file_ids']
        
        # Update files to archived status
        files = File.query.filter(
            File.id.in_(file_ids),
            File.account_id == target_account.id
        ).all()
        
        for file in files:
            file.archived = True
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'Successfully archived {len(files)} files'
        })
        
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error archiving files for account {account_url}: {e}")
        return jsonify({'error': 'Failed to archive files'}), 500

@accounts_bp.route('/<account_url>/files/unarchive', methods=['POST'])
def unarchive_files(account_url):
    app.logger.info(f"Unarchiving files for account {account_url}")
    try:
        # Get target account
        target_account = Account.query.filter_by(url=account_url).first_or_404()
        
        data = request.get_json()
        if not data or 'file_ids' not in data:
            return jsonify({'error': 'No file IDs provided'}), 400
            
        file_ids = data['file_ids']
        
        # Update files to unarchived status
        files = File.query.filter(
            File.id.in_(file_ids),
            File.account_id == target_account.id
        ).all()
        
        for file in files:
            file.archived = False
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'Successfully unarchived {len(files)} files'
        })
        
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error unarchiving files for account {account_url}: {e}")
        return jsonify({'error': 'Failed to unarchive files'}), 500

@accounts_bp.route('/<account_url>/data/content', methods=['GET'])
def account_data_content(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        g.account = account  # Make account available in global context
        
        # Get directory filter
        directory = request.args.get('directory')
        if directory == 'None':
            directory = None
        
        # Get show_archived parameter and convert to boolean properly
        archived_param = request.args.get('archived', 'false')
        show_archived = archived_param.lower() in ['true', '1', 'yes', 'on']
        
        # Get page number
        page = request.args.get('page', 1, type=int)
        if page == 'None':
            page = 1
        per_page = 100
        
        # Combine base filters into a single expression
        base_filters = and_(
            File.account_id == account.id,
            not_(File.key.like('.%')),
            not_(File.key.contains('/.')),
            not_(File.key.like('__MACOSX%'))
        )
        
        # Initialize base query with base filters
        base_query = File.query.filter(base_filters)
        
        # Add directory-specific filters if needed
        if directory:
            if directory == '/':
                # For root directory, add filter for files without slashes
                base_query = base_query.filter(not_(File.key.contains('/')))
            else:
                # Remove leading slash if present for consistency
                directory = directory.lstrip('/')
                # Match files that are directly in this directory
                base_query = base_query.filter(
                    and_(
                        File.key.like(f"{directory}/%"),
                        not_(File.key.like(f"{directory}/%/%"))
                    )
                )
        
        # Get total count of files in current view (before any archived filtering)
        total_files = base_query.count()
        
        # Get total archived count for the current filter (before any archived filtering)
        total_archived = base_query.filter_by(archived=True).count()
        
        # Create display query with archived filter if needed
        display_query = base_query
        if not show_archived:
            display_query = display_query.filter(File.archived == False)
        
        # Order by last modified and paginate
        pagination = display_query.order_by(File.last_modified.desc()).paginate(
            page=page, 
            per_page=per_page,
            error_out=False
        )
        
        # Get directories for dropdown
        directories = get_directory_paths(account.id, include_all_subpaths=False)
        
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
                             now=now,
                             pagination=pagination,
                             total_files=total_files,
                             total_archived=total_archived,
                             directories=directories,
                             current_directory=directory,
                             show_archived=show_archived)
                             
    except Exception as e:
        logger.error(f"Error loading data content: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        return f"Error loading data content: {str(e)}", 500

@accounts_bp.route('/<account_url>/paths', methods=['GET'])
def get_all_paths(account_url):
    """Get all possible paths for an account, including all subpaths."""
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        
        # Get all paths including subpaths
        all_paths = get_directory_paths(account.id, include_all_subpaths=True)
        
        # Calculate sizes for each path
        for path_info in all_paths:
            path = path_info['path']
            if path:
                # Get files in this path and subpaths
                files = File.query.filter_by(account_id=account.id)\
                    .filter(File.key.like(f"{path}/%"))\
                    .filter(~File.key.like('.%'))\
                    .filter(~File.key.contains('/.'))\
                    .filter(File.archived == False)\
                    .all()
            else:
                # Root level files
                files = File.query.filter_by(account_id=account.id)\
                    .filter(~File.key.like('.%'))\
                    .filter(~File.key.contains('/.'))\
                    .filter(~File.key.contains('/'))\
                    .filter(File.archived == False)\
                    .all()
            
            # Calculate total size
            total_size = sum(file.size for file in files)
            path_info['total_size'] = total_size
        
        # Add the root level (empty string) if there are files at root
        root_files = File.query.filter_by(account_id=account.id)\
            .filter(~File.key.like('.%'))\
            .filter(~File.key.contains('/.'))\
            .filter(~File.key.contains('/'))\
            .filter(File.archived == False)\
            .count()
        
        if root_files > 0:
            root_size = File.query.filter_by(account_id=account.id)\
                .filter(~File.key.like('.%'))\
                .filter(~File.key.contains('/.'))\
                .filter(~File.key.contains('/'))\
                .filter(File.archived == False)\
                .with_entities(db.func.sum(File.size))\
                .scalar() or 0
            all_paths.insert(0, {'path': '', 'total_files': root_files, 'total_archived': 0, 'total_size': root_size})
        
        return jsonify({
            'paths': all_paths,
            'success': True
        })
    except Exception as e:
        logging.error(f"Error getting paths for {account_url}: {e}")
        return jsonify({'error': str(e), 'success': False}), 500
    
@accounts_bp.route('/<account_url>/gateways', methods=['GET'])
def dashboard_gateways(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        analytics = get_analytics(account.id)  # Get analytics for gateway counts
        if not analytics:
            return "Error loading analytics", 500
            
        # Get current time and 30 days ago
        now = datetime.now(timezone.utc)
        thirty_days_ago = now - timedelta(days=30)
        
        # Optimized query to get the most recent ping from each unique gateway name
        # This uses the indexes we just created
        from sqlalchemy import func
        
        # Use window function for better performance
        latest_gateways = db.session.query(
            Gateway.id,
            Gateway.ip_address,
            Gateway.name,
            Gateway.created_at
        ).filter(
            Gateway.account_id == account.id,
            Gateway.created_at >= thirty_days_ago
        ).order_by(Gateway.name, Gateway.created_at.desc())\
         .distinct(Gateway.name)\
         .limit(20)\
         .all()
            
        return render_template('components/dashboard_gateways.html',
                             account=account,
                             gateways=latest_gateways,
                             analytics=analytics)
    except Exception as e:
        logging.error(f"Error loading dashboard gateways for {account_url}: {e}")
        return "Error loading gateway activity", 500

@accounts_bp.route('/<account_url>/nodes', methods=['GET'])
def dashboard_nodes(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        
        # Get current time and 30 days ago
        now = datetime.now(timezone.utc)
        thirty_days_ago = now - timedelta(days=30)
        
        # Get unique nodes with their most recent entry from last 30 days
        # Using a window function to get the latest entry per UUID
        from sqlalchemy import func
        
        # Get unique nodes with their most recent seen and connected entries from last 30 days
        from sqlalchemy import func
        
        # Get all unique UUIDs that have been seen in the last 30 days
        unique_uuids = db.session.query(Node.uuid).join(Gateway).filter(
            Gateway.account_id == account.id,
            Node.created_at >= thirty_days_ago
        ).distinct().all()
        
        nodes_list = []
        for (uuid,) in unique_uuids:
            # Get the most recent seen entry for this UUID
            latest_seen = db.session.query(
                Node.uuid,
                Node.created_at,
                Node.device_id,
                Node.battery_level,
                Node.alert,
                Gateway.name.label('gateway_name'),
                Gateway.ip_address.label('gateway_ip')
            ).join(Gateway).filter(
                Gateway.account_id == account.id,
                Node.uuid == uuid,
                Node.created_at >= thirty_days_ago
            ).order_by(Node.created_at.desc()).first()
            
            # Get the most recent connected entry for this UUID
            latest_connected = db.session.query(
                Node.created_at,
                Node.device_id,
                Node.battery_level,
                Node.alert
            ).join(Gateway).filter(
                Gateway.account_id == account.id,
                Node.uuid == uuid,
                Node.created_at >= thirty_days_ago,
                Node.was_connected == True
            ).order_by(Node.created_at.desc()).first()
            
            if latest_seen:
                nodes_list.append({
                    'uuid': uuid,
                    'device_id': latest_connected.device_id if latest_connected else latest_seen.device_id,  # Pass raw device_id
                    'battery': latest_connected.battery_level if latest_connected else latest_seen.battery_level,
                    'alert': latest_connected.alert if latest_connected else latest_seen.alert,
                    'scanned_by': latest_seen.gateway_name,
                    'last_seen': latest_seen.created_at,
                    'last_connected': latest_connected.created_at if latest_connected else None
                })
        
        # Sort by last seen (most recent first)
        nodes_list.sort(key=lambda x: x['last_seen'], reverse=True)
        
        # Limit to 50 nodes to prevent performance issues
        nodes_list = nodes_list[:50]
        
        # Format the data for the template
        formatted_nodes = []
        for node in nodes_list:
            # Use real battery level if available, otherwise use 0
            battery_percentage = node['battery'] if node['battery'] is not None else 0
            
            formatted_nodes.append({
                'uuid': node['uuid'],
                'device_id': node['device_id'],
                'battery': battery_percentage,
                'alert': node['alert'],
                'scanned_by': node['scanned_by'],
                'last_seen': node['last_seen'],
                'last_connected': node['last_connected']
            })
        
        return render_template('components/dashboard_nodes.html',
                             account=account,
                             nodes=formatted_nodes)
    except Exception as e:
        logging.error(f"Error loading dashboard nodes for {account_url}: {e}")
        return "Error loading node activity", 500

@accounts_bp.route('/<account_url>/nodes/<uuid>/battery-history', methods=['GET'])
def node_battery_history(account_url, uuid):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        
        # Get battery history for this node UUID
        # Only include entries where battery_level is not None and not 0
        battery_history = db.session.query(
            Node.created_at,
            Node.battery_level
        ).join(Gateway).filter(
            Gateway.account_id == account.id,
            Node.uuid == uuid,
            Node.battery_level.isnot(None),
            Node.battery_level > 0
        ).order_by(Node.created_at.asc()).all()
        
        if not battery_history:
            return jsonify({
                'success': False,
                'error': 'No battery history available'
            })
        
        # Format data for plotting
        data = {
            'timestamps': [entry.created_at.isoformat() for entry in battery_history],
            'battery_levels': [entry.battery_level for entry in battery_history]
        }
        
        return jsonify({
            'success': True,
            'data': data
        })
        
    except Exception as e:
        logging.error(f"Error loading battery history for {account_url}/{uuid}: {e}")
        return jsonify({
            'success': False,
            'error': 'Error loading battery history'
        }), 500

@accounts_bp.route('/<account_url>/nodes/<node_uuid>/clear-alerts', methods=['POST'])
def clear_node_alerts(account_url, node_uuid):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        
        # Update all nodes with this UUID to clear their alerts
        # First get the node IDs that belong to this account
        node_ids = db.session.query(Node.id).join(Gateway).filter(
            Gateway.account_id == account.id,
            Node.uuid == node_uuid
        ).all()
        
        # Then update those nodes
        updated_count = Node.query.filter(Node.id.in_([n.id for n in node_ids])).update({
            Node.alert: None
        })
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'Cleared alerts for {updated_count} node entries',
            'updated_count': updated_count
        }), 200
        
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error clearing alerts for node {node_uuid} in {account_url}: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
    
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
        start_date = end_date - timedelta(days=29)  # 30 days ago
        
        # Create a list of all dates in range (including today)
        date_range = [(start_date + timedelta(days=x)).date() for x in range(30)]
        
        # Convert timestamps to UTC for database query
        start_date_utc = start_date.astimezone(timezone.utc)
        end_date_utc = end_date.replace(hour=23, minute=59, second=59, microsecond=999999).astimezone(timezone.utc)
        
        # Use database aggregation with timezone conversion
        # This is much more efficient than fetching all files
        
        # Query to get daily counts using database aggregation
        daily_counts_query = db.session.query(
            func.date_trunc('day', 
                func.timezone(account_tz, File.last_modified)
            ).label('date'),
            func.count(File.id).label('count')
        ).filter(
            File.account_id == account.id,
            ~File.key.like('.%'),
            ~File.key.contains('/.'),
            File.last_modified >= start_date_utc,
            File.last_modified <= end_date_utc
        ).group_by(
            func.date_trunc('day', func.timezone(account_tz, File.last_modified))
        ).all()
        
        # Initialize counts for all days using string dates as keys
        daily_counts = {date.strftime('%Y-%m-%d'): 0 for date in date_range}
        
        # Fill in the actual counts from database
        for date_obj, count in daily_counts_query:
            if date_obj:
                date_str = date_obj.strftime('%Y-%m-%d')
                if date_str in daily_counts:
                    daily_counts[date_str] = count
        
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
        if plot.type in ['timeline', 'timebin']:  # Handle both timeline and timebin
            y_data = request.form.get('y_data')
            if not y_data:
                flash('Data Column is required for timeline plots.', 'error')
                return redirect(url_for('accounts.account_plots', account_url=account_url))
                
            config = {
                'x_data': plot.source.datetime_column,
                'y_data': y_data
            }
            
            # Add timebin specific config
            if plot.type == 'timebin':
                bin_hrs = request.form.get('bin_hrs', type=int, default=24)
                mean_nsum = request.form.get('mean_nsum') == 'on'  # True if checked
                
                # Validate bin_hrs
                if not bin_hrs or bin_hrs < 1 or bin_hrs > 24:
                    flash('Bin hours must be between 1 and 24.', 'error')
                    return redirect(url_for('accounts.account_plots', account_url=account_url))
                    
                config.update({
                    'bin_hrs': bin_hrs,
                    'mean_nsum': mean_nsum
                })
                
        elif plot.type in ['box', 'bar', 'table']:
            y_data = request.form.get('y_data')
            if not y_data:
                flash('Data column is required.', 'error')
                return redirect(url_for('accounts.account_plots', account_url=account_url))
                
            config = {
                'y_data': y_data
            }
            
        # Update config using the new property
        plot.config_json = config
        
        # Update advanced options using the new property
        advanced_options = []
        if request.form.get('accumulate') == 'on':
            advanced_options.append('accumulate')
        if request.form.get('last_value') == 'on':
            advanced_options.append('last_value')
        
        plot.advanced_json = advanced_options
        
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
        if not data:
            return jsonify({
                'error': 'No data provided',
                'status': 400
            }), 400
            
        # Get account and source directly using URL and ID
        account = Account.query.filter_by(url=account_url).first_or_404()
        source = Source.query.filter_by(id=source_id, account_id=account.id).first_or_404()
        
        # Get key and error fields, with empty string defaults
        key = data.get('key', '').strip()
        error = data.get('error', '').strip()
        
        # Update source timestamp
        source.last_updated = datetime.now(timezone.utc)
        
        # Handle error state (including empty key)
        if error or not key:
            source.state = 'error'
            source.error = error if error else 'Empty or missing key field'
            db.session.commit()
            
            return jsonify({
                'message': 'Source error state updated',
                'status': 200
            })
        
        logging.info(f"Processing Lambda callback for source: {source.name} (ID: {source.id})")
        
        # Update source fields for success case
        source.state = 'success'
        source.error = None  # Clear any previous error
        
        # Get matching files and calculate max_path_level
        matching_files = list_source_files(account, source)
        max_level = 0
        for file in matching_files:
            path_segments = file.key.strip('/').split('/')
            max_level = max(max_level, len(path_segments))
        
        source.max_path_level = max_level
        
        # Handle file record only if we have a valid key and size
        if 'size' in data:
            file = File.query.filter_by(account_id=account.id, key=key).first()
            if not file:
                logging.info(f"Creating new file record for key: {key}")
                file = File(
                    account_id=account.id,
                    key=key,
                    url=generate_s3_url(account.settings.bucket_name, key),
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

## Base Structure (Always Present)
Fields:
    name: string (required) - Name of the gateway
    nodes: array of strings (required) - Array of node UUIDs associated with this gateway
    connected_node: integer (optional) - 1-indexed position of successfully connected node in nodes array (0 if none)

## Optional Fields (Enhanced Node Data)
The following fields are only included when the connected node's firmware supports them:
    device_id: string (optional) - Device identifier for the connected node (e.g., "046")
    battery_level: integer (optional) - Battery percentage (0-100) of the connected node
    alert: string (optional) - Alert message from the connected node

## Example Request Body (Backwards Compatible):
    {
        "name": "Gateway1",
        "nodes": ["550e8400-e29b-41d4-a716-446655440000", "550e8400-e29b-41d4-a716-446655440001"]
    }

## Example Request Body (Enhanced):
    {
        "name": "Gateway_12345",
        "nodes": ["AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66"],
        "connected_node": 2,
        "device_id": "046",
        "battery_level": 15,
        "alert": "Low battery warning!"
    }

## Example curl (Backwards Compatible):
    curl -X POST http://127.0.0.1:5000/<account_url>/gateway \
        -H "Content-Type: application/json" \
        -d '{"name": "Gateway1", "nodes": ["550e8400-e29b-41d4-a716-446655440000"]}'

## Example curl (Enhanced):
    curl -X POST http://127.0.0.1:5000/<account_url>/gateway \
        -H "Content-Type: application/json" \
        -d '{"name": "Gateway_12345", "nodes": ["AA:BB:CC:DD:EE:FF", "11:22:33:44:55:66"], "connected_node": 2, "device_id": "046", "battery_level": 15, "alert": "Low battery warning!"}'

## Response:
    {
        "message": "Gateway and nodes updated successfully",
        "gateway_id": 1,
        "node_count": 2,
        "connected_node_updated": true,
        "node_data_updated": true
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
        
        # Track connection updates
        connected_node_updated = False
        node_data_updated = False
        
        # Create new node records for each UUID
        for i, uuid in enumerate(node_uuids):
            node = Node(
                gateway_id=gateway.id,
                uuid=uuid,
                created_at=datetime.now(timezone.utc)
            )
            db.session.add(node)
        
        # Handle enhanced node data if provided
        connected_node_index = data.get('connected_node', 0)
        if connected_node_index > 0 and connected_node_index <= len(node_uuids):
            # Get the connected node (1-indexed to 0-indexed conversion)
            connected_node_uuid = node_uuids[connected_node_index - 1]
            
            # Find the node in the session (it was just added)
            connected_node = None
            for node in db.session.new:
                if isinstance(node, Node) and node.uuid == connected_node_uuid:
                    connected_node = node
                    break
            
            if connected_node:
                # Mark this node as the connected one
                connected_node.was_connected = True
                connected_node_updated = True
                
                # Update optional enhanced data
                if 'device_id' in data:
                    connected_node.device_id = data['device_id']
                    node_data_updated = True
                
                if 'battery_level' in data:
                    battery_level = data['battery_level']
                    if isinstance(battery_level, int) and 0 <= battery_level <= 255:
                        connected_node.battery_level = battery_level
                        node_data_updated = True
                
                if 'alert' in data:
                    connected_node.alert = data['alert']
                    node_data_updated = True
        
        # Final commit for all changes
        db.session.commit()
        
        return jsonify({
            'message': 'Gateway and nodes updated successfully',
            'gateway_id': gateway.id,
            'node_count': len(node_uuids),
            'connected_node_updated': connected_node_updated,
            'node_data_updated': node_data_updated
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