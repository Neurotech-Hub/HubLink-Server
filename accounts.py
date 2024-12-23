from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, g
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
        # Track original values
        original_access_key = settings.aws_access_key_id
        original_secret_key = settings.aws_secret_access_key
        original_bucket_name = settings.bucket_name

        # Update settings with new form data
        settings.aws_access_key_id = request.form['aws_access_key_id']
        settings.aws_secret_access_key = request.form['aws_secret_access_key']
        settings.bucket_name = request.form['bucket_name']
        settings.dt_rule = request.form['dt_rule']
        settings.max_file_size = int(request.form['max_file_size'])
        settings.use_cloud = request.form['use_cloud'] == 'true'
        settings.delete_scans = request.form['delete_scans'] == 'true'
        settings.delete_scans_days_old = int(request.form['delete_scans_days_old']) if request.form['delete_scans_days_old'] else None
        settings.delete_scans_percent_remaining = int(request.form['delete_scans_percent_remaining']) if request.form['delete_scans_percent_remaining'] else None
        settings.device_name_includes = request.form['device_name_includes']
        settings.alert_file_starts_with = request.form['alert_file_starts_with']
        settings.alert_email = request.form['alert_email']
        settings.node_payload = request.form['node_payload']

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

# Route to delete an account
@accounts_bp.route('/<account_url>/delete', methods=['POST'])
def delete_account(account_url):
    account_to_delete = Account.query.filter_by(url=account_url).first_or_404()
    settings_to_delete = Setting.query.filter_by(account_id=account_to_delete.id).first()
    try:
        if settings_to_delete:
            db.session.delete(settings_to_delete)
        db.session.delete(account_to_delete)
        db.session.commit()
        logging.debug(f"Account and settings deleted for account URL {account_url}")
        return redirect(url_for('index'))
    except Exception as e:
        db.session.rollback()
        logging.error(f"There was an issue deleting the account: {e}")
        return "There was an issue deleting the account."

@accounts_bp.route('/<account_url>/data', methods=['GET'])
@accounts_bp.route('/<account_url>/data/<device_id>', methods=['GET'])
def account_data(account_url, device_id=None):
    g.title = "Data"
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        account.count_page_loads += 1
        db.session.commit()
        # Get total_limit from URL parameters, default to 100 if not provided
        total_limit = request.args.get('total_limit', 100, type=int)
        
        # Get recent files for the account, optionally filtered by device_id
        recent_files = get_latest_files(account.id, total=total_limit, device_id=device_id)
        
        # Retrieve a list of unique device IDs for display in the template
        unique_devices = get_unique_devices(account.id)
        
        return render_template(
            'data.html',
            account=account,
            recent_files=recent_files,
            unique_devices=unique_devices,
            device_id=device_id,
            total_limit=total_limit
        )
    except Exception as e:
        logging.error(f"Error loading data for {account_url} and device {device_id}: {e}")
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

@accounts_bp.route('/<account_url>/rebuild', methods=['GET'])
def rebuild(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        settings = Setting.query.filter_by(account_id=account.id).first_or_404()

        # Get count of new files from rebuild operation
        new_files = rebuild_S3_files(settings)
        if new_files > 0:
            account.count_uploaded_files += new_files
            logging.info(f"Added {new_files} new files for account {account.id}")
            db.session.commit()

        return jsonify({
            "message": "Rebuild completed successfully",
            "new_files": new_files
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
        
        # Get all files for this account
        files = File.query.filter_by(account_id=account.id).all()
        
        # Prepare data for charts
        file_uploads = [file.last_modified.isoformat() for file in files if file.last_modified]
        
        # Get device upload counts
        device_counts = {}
        for file in files:
            device = file.key.split('/')[0]  # Get device name from file key
            device_counts[device] = device_counts.get(device, 0) + 1
        
        devices = list(device_counts.keys())
        device_upload_counts = [device_counts[device] for device in devices]
        
        # Get recent gateway activity
        gateways = Gateway.query.filter_by(account_id=account.id)\
            .order_by(Gateway.created_at.desc())\
            .limit(10)\
            .all()
        
        db.session.commit()
        
        return render_template(
            'dashboard.html',
            account=account,
            file_uploads=file_uploads,
            devices=devices,
            device_upload_counts=device_upload_counts,
            gateways=gateways
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
            layout_plot_ids = [item['plotId'] for item in json.loads(layout.config)]
            for source in sources:
                for plot in source.plots:
                    if plot.id in layout_plot_ids:
                        plot_names.append(f"{source.name}: {plot.name}")
            layout_plot_names[layout.id] = plot_names

        files = File.query.filter_by(account_id=account.id).all()
        file_keys = [file.key for file in files]
        dir_patterns = generate_directory_patterns(file_keys)
        
        return render_template('plots.html', 
                          account=account, 
                          sources=sources,
                          plot_data=plot_data,  # Add plot_data to template context
                          layout_plot_names=layout_plot_names,
                          dir_patterns=dir_patterns,
                          recent_files=recent_files)
                       
    except Exception as e:
        print(f"Error in account_plots: {str(e)}")
        print(f"Traceback: {traceback.format_exc()}")
        logging.error(f"Error loading plots for {account_url}: {e}")
        return "There was an issue loading the plots page.", 500

def generate_directory_patterns(file_keys):
    dir_patterns = set()
    
    # Add root level pattern
    dir_patterns.add('*')
    
    for key in file_keys:
        # Skip hidden files and directories
        if any(part.startswith('.') for part in key.split('/')):
            continue
            
        # Split the key by '/' and remove the filename
        parts = key.split('/')[:-1]
        
        # Generate patterns for each directory level
        for i in range(len(parts)):
            # Pattern that matches specific directories up to this level
            exact_pattern = '/'.join(parts[:i+1]) + '/*'
            dir_patterns.add(exact_pattern)
            
            # Pattern with wildcard for the last directory
            if i > 0:
                wildcard_pattern = '/'.join(parts[:i]) + '/[^/]+/*'
                dir_patterns.add(wildcard_pattern)
    
    return sorted(list(dir_patterns))

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
        # First verify the account exists and the plot belongs to it
        account = Account.query.filter_by(url=account_url).first_or_404()
        plot = Plot.query.join(Source).filter(
            Plot.id == plot_id,
            Source.account_id == account.id
        ).first_or_404()
        
        # Store plot name for flash message
        plot_name = plot.name
        
        # Delete the plot
        db.session.delete(plot)
        
        # Update each layout's config to remove the plot entry
        for layout in account.layouts:
            config = json.loads(layout.config)
            # Filter out the plot with the given plot_id
            updated_config = [entry for entry in config if entry.get('plotId') != plot_id]
            layout.config = json.dumps(updated_config)
        
        db.session.commit()
        
        flash(f'Plot "{plot_name}" deleted successfully.', 'success')
        return redirect(url_for('accounts.account_plots', account_url=account_url))
    
    except Exception as e:
        db.session.rollback()
        logging.error(f"Error deleting plot {plot_id} for account {account_url}: {e}")
        flash('Error deleting plot.', 'error')
        return redirect(url_for('accounts.account_plots', account_url=account_url))

@accounts_bp.route('/<account_url>/layout/<int:layout_id>', methods=['POST'])
def save_layout(account_url, layout_id):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        layout = Layout.query.filter_by(id=layout_id, account_id=account.id).first_or_404()
        data = request.get_json()
        
        # Update layout name if provided
        if 'name' in data:
            layout.name = data['name']
        
        # Ensure plotId is stored as integer in config
        config = data['config']
        for item in config:
            item['plotId'] = int(item['plotId'])  # Convert to integer
            
        layout.config = json.dumps(config)
        db.session.commit()
        
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error saving layout {layout_id} for account {account_url}: {e}")
        return jsonify({'success': False, 'error': str(e)})

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
        
        # Get all plots for this account
        plots = []
        for source in Source.query.filter_by(account_id=account.id).all():
            plots.extend(source.plots)
        
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