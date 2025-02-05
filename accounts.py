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

# Create the Blueprint for account-related routes
accounts_bp = Blueprint('accounts', __name__)

logger = logging.getLogger(__name__)
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
        account.count_page_loads += 1
        db.session.commit()
        
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
        account.count_page_loads += 1
        db.session.commit()
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
        account.count_settings_updated += 1
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

        # Track original values
        original_access_key = settings.aws_access_key_id
        original_secret_key = settings.aws_secret_access_key
        original_bucket_name = settings.bucket_name

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
        account.count_page_loads += 1
        db.session.commit()
        return render_template('data.html', account=account, directory=directory)
    except Exception as e:
        logging.error(f"Error loading data for {account_url}: {e}")
        return "There was an issue loading the data page.", 500

@accounts_bp.route('/<account_url>/files', methods=['POST'])
def check_files(account_url):
    try:
        logging.debug(f"Received request for checking files for account: {account_url}")
        account = Account.query.filter_by(url=account_url).first_or_404()
        request_data = request.get_json()
        logging.debug(f"Request data: {request_data}")

        if not request_data or 'files' not in request_data:
            return jsonify({"error": "Invalid input. JSON body must contain 'files'."}), 400
        
        files = request_data.get('files', [])
        
        if not isinstance(files, list) or not all(isinstance(file, dict) and 'filename' in file and 'size' in file for file in files):
            logging.error("Invalid input structure for 'files'. Must be a list of dictionaries with 'filename' and 'size'.")
            return jsonify({"error": "Invalid input. 'files' must be a list of dictionaries with 'filename' and 'size' keys."}), 400

        # Get the current time in UTC
        current_time = datetime.now(timezone.utc)
        logger.info(f"Setting last_checked to UTC time: {current_time} for files: {[f['filename'] for f in files]}")
        
        # Update last_checked for all files being checked
        for file_data in files:
            file = File.query.filter_by(account_id=account.id, key=file_data['filename']).first()
            if file:
                file.last_checked = current_time
        
        db.session.commit()

        # Delegate to existing function to check file existence
        result = do_files_exist(account.id, files)
        logging.debug(f"File existence result: {result}")

        return jsonify({"exists": result})

    except Exception as e:
        logging.error(f"Error in '/{account_url}/files' endpoint: {e}")
        logging.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({"error": "There was an issue processing your request."}), 500

@accounts_bp.route('/<account_url>/source/<int:source_id>.json', methods=['GET'])
def get_source_files(account_url, source_id):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        source = Source.query.filter_by(id=source_id, account_id=account.id).first_or_404()
            
        # Get matching files for this source
        matching_files = list_source_files(account, source)
        
        # Convert files to dict for JSON response
        files_data = [{'key': f.key, 'size': f.size, 
                      'last_modified': f.last_modified.isoformat() if f.last_modified else None} 
                     for f in matching_files]
        
        return jsonify(files_data)
        
    except Exception as e:
        logging.error(f"Error getting source files: {e}")
        return jsonify({'error': str(e)}), 500

@accounts_bp.route('/<account_url>/rebuild', methods=['GET'])
def rebuild(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        settings = Setting.query.filter_by(account_id=account.id).first_or_404()

        # Get list of affected files from rebuild operation
        affected_files = rebuild_S3_files(settings)
        refresh_count = 0
        
        if affected_files:
            # Update account counter
            account.count_uploaded_files += len(affected_files)
            
            # Get all sources for this account
            sources = Source.query.filter_by(account_id=account.id).all()
            affected_sources = set()  # Use set to ensure uniqueness
            
            # Create a set of affected file keys for efficient lookup
            affected_file_keys = {file.key for file in affected_files}
            
            # For each source, check if any of its matching files were affected
            for source in sources:
                try:
                    matching_files = list_source_files(account, source)
                    # Check if any matching files were affected by the rebuild
                    if any(file.key in affected_file_keys for file in matching_files):
                        affected_sources.add(source)
                except Exception as e:
                    logging.error(f"Error matching pattern for source {source.id}: {e}")
                    continue
            
            # Set do_update=True for each affected source
            for source in affected_sources:
                try:
                    source.do_update = True
                    refresh_count += 1
                    logging.info(f"Marked source {source.id} for update")
                except Exception as e:
                    logging.error(f"Error marking source {source.id} for update: {e}")
                    continue

            db.session.commit()
            logging.info(f"Added/updated {len(affected_files)} files and marked {refresh_count} sources for refresh for account {account.id}")

        return jsonify({
            "message": "Rebuild completed successfully",
            "affected_files": len(affected_files),
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
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        account.count_page_loads += 1
        db.session.commit()
        
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
        
        return render_template('plots.html', 
                             account=account, 
                             sources=sources,
                             source_paths=source_paths,
                             layout_plot_names=layout_plot_names,
                             directories=directories)
                             
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
        account.count_page_loads += 1
        db.session.commit()
        
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
        account.count_page_loads += 1
        db.session.commit()
        
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
        
        # Get files query with directory filter if provided
        files_query = File.query.filter_by(account_id=account.id)\
            .filter(~File.key.like('.%'))\
            .filter(~File.key.contains('/.'))\
            .filter(~File.key.like('__MACOSX%'))  # Also exclude macOS metadata
        
        if directory:
            # For root directory
            if directory == '/':
                files_query = files_query.filter(~File.key.contains('/'))
            else:
                # Remove leading slash if present for consistency
                directory = directory.lstrip('/')
                # Match files that are directly in this directory
                files_query = files_query.filter(
                    File.key.like(f"{directory}/%")  # Starts with directory/
                ).filter(
                    ~File.key.like(f"{directory}/%/%")  # But doesn't have another slash after
                )
        
        # Order by last modified and paginate
        pagination = files_query.order_by(File.last_modified.desc()).paginate(
            page=page, 
            per_page=per_page,
            error_out=False
        )
        
        # Get directories for dropdown
        directories = get_directory_paths(account.id)
        
        # Convert files to dicts to ensure proper timezone handling
        now = datetime.now(timezone.utc)
        files = []
        for file in pagination.items:
            file_dict = file.to_dict()
            # Add any additional fields needed for the template
            file_dict['id'] = file.id
            
            # Ensure datetime objects have timezone info
            if file.last_modified:
                if file.last_modified.tzinfo is None:
                    last_modified = file.last_modified.replace(tzinfo=timezone.utc)
                else:
                    last_modified = file.last_modified
                file_dict['raw_last_modified'] = last_modified
                file_dict['last_modified'] = last_modified  # Pass the datetime object directly
            else:
                file_dict['raw_last_modified'] = None
                file_dict['last_modified'] = None
                
            # Handle last_checked similarly
            if file.last_checked:
                if file.last_checked.tzinfo is None:
                    last_checked = file.last_checked.replace(tzinfo=timezone.utc)
                else:
                    last_checked = file.last_checked
                file_dict['last_checked'] = last_checked
            else:
                file_dict['last_checked'] = None
                
            files.append(file_dict)
        
        return render_template('components/data_content.html',
                             account=account,
                             recent_files=files,
                             pagination=pagination,
                             now=now,
                             directory=directory,
                             current_directory=directory,
                             directories=directories)
                             
    except Exception as e:
        logger.error(f"Error loading data content: {e}")
        return "Error loading data content", 500
    
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
        # Get files and format dates with account's timezone
        files = File.query.filter_by(account_id=account.id)\
            .filter(~File.key.like('.%'))\
            .filter(~File.key.contains('/.'))
        
        # Format dates in account's timezone, using absolute format for the chart
        file_uploads = [format_datetime(f.last_modified, account.settings.timezone, 'absolute') 
                       for f in files.order_by(File.last_modified.desc()).all()]
        
        return render_template('components/dashboard_uploads.html',
                             account=account,
                             file_uploads=file_uploads)
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
            .all()
        
        # Process directory data
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
                            'subdirs': set()
                        }
                    dir_data[dir_path]['count'] += 1
                    dir_data[dir_path]['size'] += file.size
                    
                    # Update latest date if this file is newer
                    if not dir_data[dir_path]['latest_date'] or \
                       (file.last_modified and file.last_modified > dir_data[dir_path]['latest_date']):
                        dir_data[dir_path]['latest_date'] = file.last_modified
                    
                    # Add subdirectories
                    for i in range(1, len(path_parts)):
                        subdir = '/'.join(path_parts[:i])
                        if subdir != dir_path:
                            dir_data[dir_path]['subdirs'].add(subdir)
        
        # Convert directory data for the template
        dir_names = sorted(dir_data.keys())
        dir_counts = [dir_data[d]['count'] for d in dir_names]
        dir_details = {
            d: {
                'size': dir_data[d]['size'],
                'latest_date': format_datetime(dir_data[d]['latest_date'], account.settings.timezone, 'absolute') if dir_data[d]['latest_date'] else None,
                'subdir_count': len(dir_data[d]['subdirs'])
            } for d in dir_names
        }
        
        return render_template('components/dashboard_dirs.html',
                             account=account,
                             dir_names=dir_names,
                             dir_counts=dir_counts,
                             dir_details=dir_details,
                             common_prefix=None)
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