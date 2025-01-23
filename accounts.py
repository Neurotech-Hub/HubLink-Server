from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, g, send_file, current_app, session
from models import db, Account, Setting, File, Gateway, Source, Plot, Layout
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
from utils import admin_required, get_analytics  # Import get_analytics
import dateutil.parser as parser

# Create the Blueprint for account-related routes
accounts_bp = Blueprint('accounts', __name__)

@accounts_bp.before_request
def load_blueprint_user():
    g.user = None
    if 'admin_id' in session:
        account = db.session.get(Account, session['admin_id'])
        if account and account.is_admin:
            g.user = account

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
                # Add a row for the gateway
                gateway = Gateway(
                    account_id=account.id,
                    ip_address=ip_address,
                    name=gateway_name,
                    created_at=datetime.now(timezone.utc)
                )
                
                # Add the new gateway to the session and commit
                db.session.add(gateway)
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
        settings.use_cloud = request.form['use_cloud'] == 'true'
        settings.gateway_manages_memory = request.form['gateway_manages_memory'] == 'true'
        settings.device_name_includes = device_name_includes

        # Check if any of the AWS settings were updated
        if (original_access_key != settings.aws_access_key_id or
                original_secret_key != settings.aws_secret_access_key or
                original_bucket_name != settings.bucket_name):
            rebuild_S3_files(settings)  # Call update_S3_files if any of the AWS settings changed

        db.session.commit()
        flash("Settings updated successfully.", "success")
        logging.debug(f"Settings updated for account URL {account_url}")
        return redirect(url_for('accounts.account_settings', account_url=account_url))
    except Exception as e:
        db.session.rollback()
        flash("There was an issue updating the settings.", "error")
        logging.error(f"There was an issue updating the settings: {e}")
        return "There was an issue updating the settings."

def get_directory_paths(account_id):
    """Get unique top-level directory paths from files for an account."""
    files = File.query.filter_by(account_id=account_id)\
        .filter(~File.key.like('.%'))\
        .filter(~File.key.contains('/.')) \
        .all()
    directories = set()
    
    for file in files:
        # Get the top-level directory
        path_parts = file.key.split('/')
        if len(path_parts) > 1:  # If there's at least one directory
            top_dir = path_parts[0]
            # Only add if it doesn't start with a dot
            if not top_dir.startswith('.'):
                directories.add(top_dir)
    
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
        return render_template('data.html', account=account)
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

def s3_pattern_to_regex(pattern, include_subdirs=False):
    """Convert a directory filter pattern to a regex pattern."""
    # Handle root directory case
    if pattern == '/':
        return '^[^/]+$' if not include_subdirs else '^.+$'
    
    # Strip leading slash and escape special regex characters
    pattern = pattern.lstrip('/')
    pattern = re.escape(pattern)
    
    # Add pattern for matching files in the directory
    if include_subdirs:
        pattern = f"^{pattern}/.*$"  # Match any files in directory or subdirectories
    else:
        pattern = f"^{pattern}/[^/]+$"  # Match only files directly in directory
        
    return pattern

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
            
            # For each source, check if any affected files match its pattern
            for source in sources:
                try:
                    pattern = s3_pattern_to_regex(source.directory_filter, source.include_subdirs)
                    regex = re.compile(pattern)
                    
                    # Check each affected file against this source's pattern
                    for file in affected_files:
                        if regex.match(file.key):
                            affected_sources.add(source)
                            break  # One matching file is enough to trigger a refresh
                except Exception as e:
                    logging.error(f"Error matching pattern for source {source.id}: {e}")
                    continue
            
            # Trigger refresh for each affected source
            for source in affected_sources:
                try:
                    success, error = initiate_source_refresh(source, settings)
                    if success:
                        refresh_count += 1
                        logging.info(f"Initiated refresh for source {source.id}")
                    else:
                        logging.error(f"Failed to refresh source {source.id}: {error}")
                except Exception as e:
                    logging.error(f"Error refreshing source {source.id}: {e}")
                    continue

            db.session.commit()
            logging.info(f"Added/updated {len(affected_files)} files and triggered {refresh_count} source refreshes for account {account.id}")

        return jsonify({
            "message": "Rebuild completed successfully",
            "affected_files": len(affected_files),
            "refreshed_sources": refresh_count
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
        account.count_page_loads += 1
        
        # Get files sorted by last_modified in descending order
        files = File.query.filter_by(account_id=account.id).order_by(File.last_modified.desc()).all()
        file_keys = [file.key for file in files]
        directories = generate_directory_patterns(file_keys)  # Now returns top-level directories
        
        sources = Source.query.filter_by(account_id=account.id).all()
        recent_files = get_latest_files(account.id, 100)
        
        # Prepare layout plot names
        layout_plot_names = {}
        for layout in account.layouts:
            plot_names = []
            try:
                config = json.loads(layout.config)
                plot_ids = [int(item['plotId']) for item in config if 'plotId' in item]
                
                # Create a lookup dictionary for all plots
                plot_lookup = {}
                for source in sources:
                    for plot in source.plots:
                        plot_lookup[plot.id] = {
                            'source_name': source.name,
                            'plot_name': plot.name,
                            'plot_type': plot.type
                        }
                
                # Get plot names in order of layout config
                for plot_id in plot_ids:
                    if plot_id in plot_lookup:
                        plot_info = plot_lookup[plot_id]
                        plot_names.append(
                            f"{plot_info['source_name']} → {plot_info['plot_name']} ({plot_info['plot_type']})"
                        )
                
            except Exception as e:
                logging.error(f"Error processing layout config: {e}")
                plot_names = ["Error loading plots"]
                
            layout_plot_names[layout.id] = plot_names

        return render_template('plots.html', 
                          account=account, 
                          sources=sources,
                          layout_plot_names=layout_plot_names,
                          directories=directories,  # Changed from dir_patterns to directories
                          recent_files=recent_files)
                       
    except Exception as e:
        print(f"Error in account_plots: {str(e)}")
        print(f"Traceback: {traceback.format_exc()}")
        logging.error(f"Error loading plots for {account_url}: {e}")
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

def initiate_source_refresh(source, settings):
    """
    Initiates a refresh for a source without any HTTP redirects.
    Returns (success, error_message) tuple.
    """
    try:
        # Reset source status
        source.success = False
        source.error = None
        source.file_id = None
        source.state = 'running'

        # Prepare payload for lambda
        payload = {
            'source': {
                'name': source.name,
                'directory_filter': source.directory_filter,
                'include_subdirs': source.include_subdirs,
                'include_columns': source.include_columns,
                'data_points': source.data_points,
                'tail_only': source.tail_only,
                'bucket_name': settings.bucket_name
            }
        }
        
        lambda_url = os.environ.get('LAMBDA_URL')
        if not lambda_url:
            raise ValueError("LAMBDA_URL environment variable not set")
            
        try:
            requests.post(lambda_url, json=payload, timeout=0.1)  # timeout right away
        except requests.exceptions.Timeout:
            # This is expected, ignore it
            pass
        
        db.session.commit()
        return True, None
        
    except Exception as e:
        error_msg = str(e)
        logging.error(f"Error refreshing source {source.id}: {error_msg}")
        if not source.error:  # Only set error if not already set
            source.error = error_msg
            source.state = 'error'
            db.session.commit()
        return False, error_msg

@accounts_bp.route('/<account_url>/source/<int:source_id>/refresh', methods=['POST'])
def refresh_source(account_url, source_id):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        source = Source.query.filter_by(id=source_id, account_id=account.id).first_or_404()
        settings = Setting.query.filter_by(account_id=account.id).first_or_404()

        success, error = initiate_source_refresh(source, settings)
        
        if success:
            flash('Source refresh initiated.', 'success')
        else:
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
        name = request.form.get('name', '').strip()
        directory_filter = request.form.get('directory_filter', '')
        include_subdirs = request.form.get('include_subdirs') == 'on'
        include_columns = request.form.get('include_columns', '')
        datetime_column = request.form.get('datetime_column', '')
        tail_only = request.form.get('tail_only') == 'on'
        data_points = request.form.get('data_points', type=int)
        groups = []  # Initialize with empty list for new sources
        
        # Create new source
        new_source = Source(
            name=name,
            directory_filter=directory_filter,
            include_subdirs=include_subdirs,
            include_columns=include_columns,
            datetime_column=datetime_column,
            tail_only=tail_only,
            data_points=data_points,
            account_id=account.id,
            state='running',
            groups=groups  # Initialize with empty list
        )
        
        db.session.add(new_source)
        db.session.commit()
        
        flash('Source created successfully', 'success')
        return redirect(url_for('accounts.account_plots', account_url=account_url))
        
    except Exception as e:
        db.session.rollback()
        flash('Error creating source', 'error')
        logging.error(f"Error creating source: {e}")
        return redirect(url_for('accounts.account_plots', account_url=account_url))

@accounts_bp.route('/<account_url>/source/<int:source_id>/delete', methods=['POST'])
def delete_source(account_url, source_id):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        source = Source.query.filter_by(id=source_id, account_id=account.id).first_or_404()
        
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
        print(f"[DEBUG] Starting create_plot for account_url: {account_url}, source_id: {source_id}")
        account = Account.query.filter_by(url=account_url).first_or_404()
        print(f"[DEBUG] Found account: {account.id}")
        
        source = Source.query.filter_by(id=source_id, account_id=account.id).first_or_404()
        print(f"[DEBUG] Found source: {source.id}, name: {source.name}")
        
        plot_name = request.form.get('name')
        plot_type = request.form.get('type')
        print(f"[DEBUG] Plot details - name: {plot_name}, type: {plot_type}")
        
        # Validate plot name
        if not plot_name:
            flash('Plot name is required.', 'error')
            return redirect(url_for('accounts.account_plots', account_url=account_url))
        
        # Build config based on plot type
        config = {}
        if plot_type == 'timeline':
            y_data = request.form.get('y_data')
            print(f"[DEBUG] Timeline plot data - y_data: {y_data}")
            
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
            
        print(f"[DEBUG] Final config: {config}")
        
        # Create new plot with JSON config
        plot = Plot(
            source_id=source_id,
            name=plot_name,
            type=plot_type,
            config=json.dumps(config),
            group_by=request.form.get('group_by', type=int, default=None)  # Handle group_by field
        )
        
        print("[DEBUG] Processing plot data...")
        # Process plot data and save it to plot.data
        plot_data = get_plot_data(plot, source, account)
        print(f"[DEBUG] Plot data processed, length: {len(str(plot_data))} chars")
        
        if not plot_data:
            print("[DEBUG] Error: No plot data generated")
            flash('Error: No data available for the selected columns.', 'error')
            return redirect(url_for('accounts.account_plots', account_url=account_url))
        
        plot.data = json.dumps(plot_data)
        db.session.add(plot)
        db.session.commit()
        print(f"[DEBUG] Plot created successfully with ID: {plot.id}")
        
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
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        plot = Plot.query.filter_by(id=plot_id, source_id=Source.query.filter_by(account_id=account.id).first().id).first_or_404()
        
        # Get all layouts for this account
        layouts = Layout.query.filter_by(account_id=account.id).all()
        
        # Update each layout's config to remove the deleted plot
        for layout in layouts:
            try:
                config = json.loads(layout.config)
                # Remove any widgets that reference the deleted plot
                config = [widget for widget in config if widget.get('plotId') != str(plot_id)]
                layout.config = json.dumps(config)
            except json.JSONDecodeError:
                logging.error(f"Failed to parse layout config for layout {layout.id}")
                continue
        
        db.session.delete(plot)
        db.session.commit()
        flash('Plot deleted successfully.', 'success')
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error deleting plot {plot_id}: {e}")
        flash('Error deleting plot.', 'error')
    
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

@accounts_bp.route('/<account_url>/layout/<int:layout_id>', methods=['GET'])
def layout_view(account_url, layout_id):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        account.count_page_loads += 1
        db.session.commit()
        layout = Layout.query.filter_by(id=layout_id, account_id=account.id).first_or_404()
        g.title = layout.name
        
        # Parse layout config
        config = json.loads(layout.config)
        required_plot_ids = [int(item['plotId']) for item in config if 'plotId' in item]

        # Get plot information for all required plots
        plot_info_arr = []
        for plot in Plot.query.filter(Plot.id.in_(required_plot_ids)).all():
            plot_info = get_plot_info(plot)
            plot_info_arr.append(plot_info)
        
        # Create a new layout object with parsed config
        layout_data = {
            'id': layout.id,
            'name': layout.name,
            'config': config,  # This is now a parsed JSON object
            'is_default': layout.is_default,
            'show_nav': layout.show_nav,
            'time_range': layout.time_range
        }
        
        return render_template('layout.html',
                           account=account,
                           layout=layout_data,
                           plot_info_arr=plot_info_arr)
    except Exception as e:
        logging.error(f"Error loading layout view for {account_url}: {e}")
        traceback.print_exc()
        return "There was an issue loading the layout view page.", 500

@accounts_bp.route('/<account_url>/layout/<int:layout_id>/edit', methods=['GET'])
def layout_edit(account_url, layout_id):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        account.count_page_loads += 1
        db.session.commit()
        layout = Layout.query.filter_by(id=layout_id, account_id=account.id).first_or_404()
        g.title = layout.name
        
        # Get all plots with their sources, ordered by source name and plot name
        plots = Plot.query.join(Source)\
            .filter(Source.account_id == account.id)\
            .order_by(Source.name, Plot.name)\
            .all()
        
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
        
        # Create new layout with empty config
        layout = Layout(
            account_id=account.id,
            name=layout_name,
            config=json.dumps([]),
            is_default=should_be_default,  # Set based on first layout or form input
            show_nav=request.form.get('show_nav') == 'on'
        )
        db.session.add(layout)
        db.session.commit()
        
        return redirect(url_for('accounts.layout_edit', account_url=account_url, layout_id=layout.id))
    except Exception as e:
        logging.error(f"Error creating layout for {account_url}: {e}")
        flash('Error creating layout.', 'error')
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
        columns = [col.strip() for col in result['header'].split(',')]
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
            files_query = files_query.filter(File.key.like(f"{directory}/%"))
        
        # Order by last modified and paginate
        pagination = files_query.order_by(File.last_modified.desc()).paginate(
            page=page, 
            per_page=per_page,
            error_out=False
        )
        
        # Get directories for dropdown
        directories = get_directory_paths(account.id)
        
        # Ensure all timestamps have timezone info
        now = datetime.now(timezone.utc)
        files = pagination.items
        for file in files:
            if file.last_modified and file.last_modified.tzinfo is None:
                file.last_modified = file.last_modified.replace(tzinfo=timezone.utc)
        
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
        return "Error loading data", 500
    
@accounts_bp.route('/<account_url>/dashboard/gateways', methods=['GET'])
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
    
@accounts_bp.route('/<account_url>/layout/grid', methods=['GET'])
def layout_grid(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        
        # Get current layout
        layout = Layout.query.filter_by(
            account_id=account.id,
            is_default=True
        ).first_or_404()
        
        # Parse layout config
        config = json.loads(layout.config)
        required_plot_ids = [int(item['plotId']) for item in config if 'plotId' in item]

        # Get plot information for all required plots
        plot_info_arr = []
        for plot in Plot.query.filter(Plot.id.in_(required_plot_ids)).all():
            plot_info = get_plot_info(plot)
            plot_info_arr.append(plot_info)
        
        # Create a new layout object with parsed config
        layout_data = {
            'id': layout.id,
            'name': layout.name,
            'config': config,  # This is now a parsed JSON object
            'is_default': layout.is_default,
            'show_nav': layout.show_nav,
            'time_range': layout.time_range
        }
        
        return render_template('components/layout_grid.html',
                             account=account,
                             layout=layout_data,
                             plot_info_arr=plot_info_arr)
    except Exception as e:
        logging.error(f"Error loading layout grid for {account_url}: {e}")
        return "Error loading layout grid", 500
    
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
        file_uploads = [f.last_modified.isoformat() for f in 
                       File.query.filter_by(account_id=account.id)
                       .filter(~File.key.like('.%'))
                       .filter(~File.key.contains('/.'))
                       .order_by(File.last_modified.desc())
                       .all()]
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
        
        # Get all files for this account, excluding hidden files
        files = File.query.filter_by(account_id=account.id)\
            .filter(~File.key.like('.%'))\
            .filter(~File.key.contains('/.')) \
            .all()
            
        dir_data = {}
        
        # Process each file
        for file in files:
            path_parts = file.key.split('/')
            if len(path_parts) == 1:  # Root level file
                dir_name = 'Root'
            else:
                dir_name = path_parts[0]  # First directory level
                
            if dir_name not in dir_data:
                dir_data[dir_name] = {
                    'count': 0,
                    'size': 0,
                    'latest_date': None,
                    'subdirs': set()
                }
            
            dir_data[dir_name]['count'] += 1
            dir_data[dir_name]['size'] += file.size
            
            # Update latest modification date
            if file.last_modified:
                if not dir_data[dir_name]['latest_date'] or \
                   file.last_modified > dir_data[dir_name]['latest_date']:
                    dir_data[dir_name]['latest_date'] = file.last_modified
            
            # Count subdirectories
            if len(path_parts) > 2:  # Has subdirectories
                dir_data[dir_name]['subdirs'].update(path_parts[1:-1])
        
        # Convert directory data for the template
        dir_names = sorted(dir_data.keys())
        dir_counts = [dir_data[d]['count'] for d in dir_names]
        dir_details = {
            d: {
                'size': dir_data[d]['size'],
                'latest_date': dir_data[d]['latest_date'].isoformat() if dir_data[d]['latest_date'] else None,
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
        db.session.commit()
        flash('Plot updated successfully', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash('Error updating plot', 'error')
        logging.error(f"Error updating plot: {e}")
        
    return redirect(url_for('accounts.account_plots', account_url=account_url))