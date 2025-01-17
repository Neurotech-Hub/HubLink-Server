from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, g, send_file
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

# Create the Blueprint for account-related routes
accounts_bp = Blueprint('accounts', __name__)

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
    """Get unique directory paths from files for an account, including all intermediate directories."""
    files = File.query.filter_by(account_id=account_id)\
        .filter(~File.key.like('.%'))\
        .filter(~File.key.contains('/.')) \
        .all()
    directories = set()
    
    for file in files:
        # Split the path into components
        path_parts = file.key.split('/')
        # Build up the path one component at a time
        current_path = ""
        for part in path_parts[:-1]:  # Exclude the filename
            if current_path:
                current_path = f"{current_path}/{part}"
            else:
                current_path = part
                
            # Only add if it doesn't start with a dot
            if not any(p.startswith('.') for p in current_path.split('/')):
                directories.add(current_path)
    
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
        
        # Pagination parameters
        page = request.args.get('page', 1, type=int)
        per_page = 30
        
        # Get recent files for the account, optionally filtered by directory
        query = File.query.filter_by(account_id=account.id)\
            .filter(~File.key.like('.%'))\
            .filter(~File.key.contains('/.'))\
            .order_by(File.last_modified.desc())
        
        if directory:
            # Escape special characters in the directory path for the LIKE query
            escaped_directory = directory.replace('%', '\\%').replace('_', '\\_')
            directory_query = query.filter(File.key.like(f"{escaped_directory}/%"))
            
            # Check if the directory has any files
            if directory_query.count() == 0:
                # If no files found in this directory, redirect to base data page
                flash(f'No files found in directory "{directory}"', 'info')
                return redirect(url_for('accounts.account_data', account_url=account_url))
            
            query = directory_query
        
        # Get paginated results
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)
        recent_files = pagination.items
        
        # Get unique directory paths for the dropdown
        directories = get_directory_paths(account.id)
        
        return render_template(
            'data.html',
            account=account,
            recent_files=recent_files,
            directories=directories,
            current_directory=directory,
            pagination=pagination
        )
    except Exception as e:
        logging.error(f"Error loading data for {account_url} and directory {directory}: {e}")
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

def s3_pattern_to_regex(pattern):
    """Convert an S3 pattern to a regex pattern."""
    # Escape special regex characters
    pattern = re.escape(pattern)
    # Convert S3 wildcards to regex patterns
    pattern = pattern.replace('\\*', '[^/]*')  # Single-level wildcard
    pattern = pattern.replace('\\[\\^/\\]\\+', '[^/]+')  # Keep [^/]+ as is
    pattern = f'^{pattern}$'  # Ensure full string match
    return pattern

@accounts_bp.route('/<account_url>/rebuild', methods=['GET'])
def rebuild(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        settings = Setting.query.filter_by(account_id=account.id).first_or_404()

        # Get list of affected files from rebuild operation
        affected_files = rebuild_S3_files(settings)
        
        if affected_files:
            # Update account counter
            account.count_uploaded_files += len(affected_files)
            
            # Get all sources for this account
            sources = Source.query.filter_by(account_id=account.id).all()
            affected_sources = set()  # Use set to ensure uniqueness
            
            # For each source, check if any affected files match its pattern
            for source in sources:
                try:
                    pattern = s3_pattern_to_regex(source.file_filter)
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
            refresh_count = 0
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
            "refreshed_sources": refresh_count if 'refresh_count' in locals() else 0
        }), 200
    except Exception as e:
        logging.error(f"Error during '/rebuild' endpoint: {e}")
        return jsonify({"error": "There was an issue processing the rebuild request."}), 500

# Route to view the account dashboard by its unique URL (page load)
@accounts_bp.route('/<account_url>', methods=['GET'])
def account_dashboard(account_url):
    g.title = "Dashboard"
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        account.count_page_loads += 1
        
        # Get analytics for this account
        analytics = get_analytics(account.id)
        if not analytics:
            flash('Error loading analytics', 'error')
            analytics = {}
        
        # Get all files for this account
        files = File.query.filter_by(account_id=account.id).all()
        
        # Prepare data for charts
        file_uploads = [file.last_modified.isoformat() for file in files if file.last_modified]
        
        # Analyze directory structure
        dir_data = {}
        common_prefix = None
        
        # First, collect all unique paths at each level
        paths_by_level = {}
        has_root_files = False
        
        for file in files:
            path_parts = file.key.split('/')
            # Skip hidden files and files in hidden directories
            if any(part.startswith('.') for part in path_parts):
                continue
            
            # Check if this is a root-level file
            if len(path_parts) == 1:  # Just a filename
                has_root_files = True
                continue
            
            # Store unique directory names at each level
            for i, part in enumerate(path_parts[:-1]):  # Exclude filename
                if i not in paths_by_level:
                    paths_by_level[i] = set()
                paths_by_level[i].add(part)
        
        # If we have root files, we need to show distribution at root level
        if has_root_files:
            display_level = 0
            paths_by_level[0] = paths_by_level.get(0, set())
            paths_by_level[0].add('Root')
        else:
            # Find the first level where paths diverge (more than one unique directory)
            display_level = 0
            for level in sorted(paths_by_level.keys()):
                if len(paths_by_level[level]) > 1:
                    display_level = level
                    break
                elif len(paths_by_level[level]) == 1:
                    display_level = level
        
        if paths_by_level or has_root_files:  # If we found any valid paths or root files
            # Group files by the display level directory
            for file in files:
                path_parts = file.key.split('/')
                
                # Skip hidden files and files in hidden directories
                if any(part.startswith('.') for part in path_parts):
                    continue
                
                # Handle root-level files
                if len(path_parts) == 1:
                    dir_name = 'Root'
                # Get the directory at the display level
                elif len(path_parts) > display_level:
                    dir_name = path_parts[display_level]
                else:
                    continue  # Skip files that don't have enough path depth
                
                if dir_name not in dir_data:
                    dir_data[dir_name] = {
                        'count': 0,
                        'size': 0,
                        'latest_date': None,
                        'subdirs': set()
                    }
                
                dir_data[dir_name]['count'] += 1
                dir_data[dir_name]['size'] += file.size
                
                # Track latest modification date
                if file.last_modified:
                    if not dir_data[dir_name]['latest_date'] or \
                       file.last_modified > dir_data[dir_name]['latest_date']:
                        dir_data[dir_name]['latest_date'] = file.last_modified
                
                # Track subdirectories
                if len(path_parts) > display_level + 1:
                    # Only track non-hidden subdirectories
                    subdirs = [d for d in path_parts[display_level + 1:-1] if not d.startswith('.')]
                    dir_data[dir_name]['subdirs'].update(subdirs)
            
            # Set the common prefix for display (excluding hidden directories)
            if display_level > 0:
                # Get the first file's path as a reference for the common prefix
                for file in files:
                    path_parts = file.key.split('/')
                    if any(part.startswith('.') for part in path_parts):
                        continue
                    if len(path_parts) > display_level:
                        common_prefix = '/'.join(path_parts[:display_level])
                        break
        
        # Convert directory data for the chart
        dir_names = sorted(dir_data.keys())  # Sort directory names alphabetically
        dir_counts = [dir_data[d]['count'] for d in dir_names]
        dir_details = {
            d: {
                'size': dir_data[d]['size'],
                'latest_date': dir_data[d]['latest_date'].isoformat() if dir_data[d]['latest_date'] else None,
                'subdir_count': len(dir_data[d]['subdirs'])
            } for d in dir_names
        }
        
        # Debug logging
        logging.info(f"Directory Names: {dir_names}")
        logging.info(f"Directory Counts: {dir_counts}")
        
        # Get recent gateway activity
        gateways = Gateway.query.filter_by(account_id=account.id)\
            .order_by(Gateway.created_at.desc())\
            .limit(100)\
            .all()
        
        db.session.commit()
        
        return render_template(
            'dashboard.html',
            account=account,
            file_uploads=file_uploads,
            dir_names=dir_names,
            dir_counts=dir_counts,
            dir_details=dir_details,
            common_prefix=common_prefix,
            gateways=gateways,
            analytics=analytics
        )
    except Exception as e:
        logging.error(f"Error loading dashboard for {account_url}: {e}")
        return "There was an issue loading the dashboard page.", 500

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

@accounts_bp.route('/<account_url>/data', methods=['POST'])
def delete_device_files(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        settings = Setting.query.filter_by(account_id=account.id).first_or_404()
        device_id = request.form.get('device_id')
        
        if not device_id:
            flash("No device specified for deletion.", "error")
            return redirect(url_for('accounts.account_data', account_url=account_url))
        
        # Delete files from S3 and database
        success, error_message = delete_device_files_from_s3(settings, device_id)
        
        if success:
            flash(f"Successfully deleted all files for device {device_id}.", "success")
        else:
            flash(f"Error deleting files: {error_message}", "error")

        rebuild_S3_files(settings)
            
        return redirect(url_for('accounts.account_data', account_url=account_url))
        
    except Exception as e:
        logging.error(f"Error deleting files for device {device_id} in account {account_url}: {e}")
        flash("There was an error deleting the files.", "error")
        return redirect(url_for('accounts.account_data', account_url=account_url))

@accounts_bp.route('/<account_url>/recent-checks', methods=['GET'])
def get_recent_checks(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        
        # Get files checked in last 30 minutes, excluding null last_checked values
        threshold = datetime.now(timezone.utc) - timedelta(minutes=30)
        recent_files = File.query.filter(
            File.account_id == account.id,
            File.last_checked.isnot(None),
            File.last_checked >= threshold
        ).all()
        
        # Return list of file keys that were recently checked
        return jsonify({
            "recent_files": [file.key for file in recent_files]
        })
    except Exception as e:
        logging.error(f"Error getting recent checks for {account_url}: {e}")
        return jsonify({"error": "There was an issue processing your request."}), 500

@accounts_bp.route('/<account_url>/plots', methods=['GET'])
def account_plots(account_url):
    g.title = "Plots"
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        account.count_page_loads += 1
        
        # Get files sorted by last_modified in descending order
        files = File.query.filter_by(account_id=account.id).order_by(File.last_modified.desc()).all()
        file_keys = [file.key for file in files]
        dir_patterns = generate_directory_patterns(file_keys)  # The order will now be preserved
        
        sources = Source.query.filter_by(account_id=account.id).all()
        recent_files = get_latest_files(account.id, 100)
        
        # Get plot data for all plots
        plot_data = []
        for source in sources:
            for plot in source.plots:
                plot_info = get_plot_info(plot)
                plot_data.append(plot_info)
        
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
                          plot_data=plot_data,
                          layout_plot_names=layout_plot_names,
                          dir_patterns=dir_patterns,
                          recent_files=recent_files)
                       
    except Exception as e:
        print(f"Error in account_plots: {str(e)}")
        print(f"Traceback: {traceback.format_exc()}")
        logging.error(f"Error loading plots for {account_url}: {e}")
        return "There was an issue loading the plots page.", 500

def generate_directory_patterns(file_keys):
    dir_patterns = []  # Change to list to maintain order
    pattern_set = set()  # Use set to track what we've added
    
    # Add root level pattern first
    dir_patterns.append('*')
    pattern_set.add('*')
    
    for key in file_keys:  # file_keys are pre-sorted by most recent
        # Skip hidden files and directories
        if any(part.startswith('.') for part in key.split('/')):
            continue
            
        # Split the key by '/' and remove the filename
        parts = key.split('/')[:-1]
        
        # Generate patterns for each directory level
        current_path = []
        for part in parts:
            current_path.append(part)
            
            # Add exact pattern for this level
            exact_pattern = '/'.join(current_path) + '/*'
            if exact_pattern not in pattern_set:
                dir_patterns.append(exact_pattern)
                pattern_set.add(exact_pattern)
            
            # Add wildcard pattern for this level (except root level)
            if len(current_path) > 1:
                wildcard_path = current_path[:-1]
                wildcard_pattern = '/'.join(wildcard_path) + '/[^/]+/*'
                if wildcard_pattern not in pattern_set:
                    # Insert wildcard pattern right after its parent pattern
                    parent_pattern = '/'.join(wildcard_path) + '/*'
                    try:
                        parent_idx = dir_patterns.index(parent_pattern)
                        dir_patterns.insert(parent_idx + 1, wildcard_pattern)
                    except ValueError:
                        dir_patterns.append(wildcard_pattern)
                    pattern_set.add(wildcard_pattern)
    
    return dir_patterns

def validate_source_data(form_data):
    """Validate source form data and return cleaned data"""
    if not form_data.get('name'):
        raise BadRequest('Source name is required.')
        
    # Clean and validate data
    data = {
        'name': form_data.get('name').strip(),
        'file_filter': form_data.get('file_filter', '*').strip(),
        'include_columns': form_data.get('include_columns', '').strip(),
        'data_points': int(form_data.get('data_points', 0)),
        'tail_only': form_data.get('tail_only') == 'on',
        'include_archive': form_data.get('include_archive') == 'on',
        'state': 'created'
    }
    
    # Validate data points range if specified
    if data['data_points'] < 0:
        raise BadRequest('Data points must be a positive number.')
    
    return data

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
                'file_filter': source.file_filter,
                'include_columns': source.include_columns,
                'data_points': source.data_points,
                'tail_only': source.tail_only,
                'include_archive': source.include_archive,
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
        
        # Validate input data
        validated_data = validate_source_data(request.form)
        
        source = Source(
            account_id=account.id,
            **validated_data
        )
        
        db.session.add(source)
        db.session.commit()
        
        # Trigger refresh for the newly created source
        settings = Setting.query.filter_by(account_id=account.id).first_or_404()
        success, error = initiate_source_refresh(source, settings)
        
        if success:
            flash('Source created and refresh initiated.', 'success')
        else:
            flash(f'Source created, but initial refresh failed: {error}', 'warning')
            
    except BadRequest as e:
        flash(str(e), 'error')
    except Exception as e:
        db.session.rollback()
        flash('Error creating source.', 'error')
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
        account = Account.query.filter_by(url=account_url).first_or_404()
        source = Source.query.filter_by(id=source_id, account_id=account.id).first_or_404()
        
        # Validate input data
        validated_data = validate_source_data(request.form)
        
        # Update source fields
        for key, value in validated_data.items():
            setattr(source, key, value)
        
        db.session.commit()
        flash('Source updated successfully.', 'success')
    except BadRequest as e:
        flash(str(e), 'error')
    except Exception as e:
        db.session.rollback()
        flash('Error updating source.', 'error')
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
        
        # Build config based on plot type
        config = {}
        if plot_type == 'timeline':
            x_data = request.form.get('x_data')
            y_data = request.form.get('y_data')
            print(f"[DEBUG] Timeline plot data - x_data: {x_data}, y_data: {y_data}")
            
            if not x_data or not y_data:
                print("[DEBUG] Error: Missing x_data or y_data for timeline plot")
                flash('X Data and Y Data are required for timeline plots.', 'error')
                return redirect(url_for('accounts.account_plots', account_url=account_url))
                
            config = {
                'x_data': x_data,
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
            config=json.dumps(config)
        )
        
        print("[DEBUG] Processing plot data...")
        # Process plot data and save it to plot.data
        plot_data = get_plot_data(plot, source, account)
        print(f"[DEBUG] Plot data processed, length: {len(str(plot_data))} chars")
        
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
        
        layout_config = json.loads(layout.config)
        required_plot_ids = [int(item['plotId']) for item in layout_config if 'plotId' in item]

        plot_info_arr = []
        for plot in Plot.query.filter(Plot.id.in_(required_plot_ids)).all():
            plot_info = get_plot_info(plot)
            plot_info_arr.append(plot_info)
        
        return render_template('layout.html',
                               account=account,
                               layout=layout,
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
    """
    Get the header (first line) of a file.
    Returns JSON with header content or error message.
    """
    try:
        # Verify account and file ownership
        account = Account.query.filter_by(url=account_url).first_or_404()
        file = File.query.filter_by(id=file_id, account_id=account.id).first_or_404()
        settings = Setting.query.filter_by(account_id=account.id).first_or_404()

        # Create temporary source object to use with get_source_file_header
        temp_source = Source(
            account_id=account.id,
            name=f"temp_{file.key}",
            file_id=file.id
        )

        # Get the header content
        header_content = get_source_file_header(settings, temp_source, num_lines=1)
        
        if header_content is None:
            return jsonify({
                'success': False,
                'error': 'Failed to read file header'
            }), 500

        return jsonify({
            'success': True,
            'header': header_content,
            'file_key': file.key
        })

    except Exception as e:
        logging.error(f"Error getting header for file {file_id}: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

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
            return jsonify({'error': 'Admin account not found'}), 500
            
        admin_settings = Setting.query.filter_by(account_id=admin_account.id).first()
        if not admin_settings:
            return jsonify({'error': 'Admin settings not found'}), 500
        
        data = request.get_json()
        file_ids = data.get('file_ids', [])
        current_directory = data.get('directory')
        
        if not file_ids:
            return jsonify({'error': 'No files selected for deletion'}), 400
            
        # Get all selected files from target account
        files = File.query.filter(File.id.in_(file_ids), File.account_id == target_account.id).all()
        if not files:
            return jsonify({'error': 'No files found'}), 404
            
        # Delete files using admin settings
        success, error_message = delete_files_from_s3(admin_settings, files)
        
        if success:
            # Rebuild S3 files using target account settings
            rebuild_S3_files(target_settings)
            
            # Add flash message before redirect
            flash('Files deleted successfully', 'success')
            
            # If we were in a directory view, check if it still has files
            if current_directory:
                # Check if directory still has files
                has_files = File.query.filter_by(account_id=target_account.id)\
                    .filter(File.key.like(f"{current_directory}/%"))\
                    .first() is not None
                
                if not has_files:
                    # Return flag to redirect to base data page
                    return jsonify({
                        'redirect': url_for('accounts.account_data', account_url=account_url)
                    })
            
            return jsonify({'success': True})
            
        else:
            return jsonify({'error': error_message}), 500
            
    except Exception as e:
        logging.error(f"Error deleting files for account {account_url}: {e}")
        return jsonify({'error': 'There was an error deleting the files'}), 500
    
# Route to delete an account
@accounts_bp.route('/<account_url>/delete', methods=['POST'])
@admin_required
def delete_account(account_url):
    try:
        # Get target account and its settings
        target_account = Account.query.filter_by(url=account_url).first_or_404()
        target_settings = Setting.query.filter_by(account_id=target_account.id).first_or_404()
        
        # Get admin account settings for AWS cleanup
        admin_account = Account.query.filter_by(is_admin=True).first()
        if not admin_account:
            flash('Admin account not found', 'error')
            return redirect(url_for('admin'))
            
        admin_settings = Setting.query.filter_by(account_id=admin_account.id).first()
        if not admin_settings:
            flash('Admin settings not found', 'error')
            return redirect(url_for('admin'))
        
        # Only attempt AWS cleanup if the target account has AWS resources configured
        if target_settings.bucket_name and target_settings.aws_access_key_id:
            try:
                cleanup_aws_resources(
                    admin_settings,  # Use admin credentials for cleanup
                    target_settings.aws_access_key_id.split('/')[-1],  # Extract username from access key
                    target_settings.bucket_name
                )
            except Exception as e:
                logging.error(f"Error cleaning up AWS resources for account {account_url}: {e}")
                flash('Error cleaning up AWS resources', 'error')
                return redirect(url_for('admin'))
        
        # Delete the account from database (this will cascade delete settings and files)
        db.session.delete(target_account)
        db.session.commit()
        flash('Account deleted successfully', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash('Error deleting account', 'error')
        logging.error(f"Error deleting account {account_url}: {e}")
        
    return redirect(url_for('admin'))