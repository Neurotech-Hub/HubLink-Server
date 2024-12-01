from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, g
from models import db, Account, Setting, File, Gateway, Source, Plot, Layout
from datetime import datetime, timedelta, timezone
import logging
from S3Manager import *
import traceback
from werkzeug.exceptions import NotFound, BadRequest
from sqlalchemy import desc
from sqlalchemy.exc import IntegrityError
import re
import requests
import os
import json
from plot_utils import process_timeseries_plot, process_metric_plot

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

# Sync endpoint, touch after gateway uploading
@accounts_bp.route('/<account_url>/sync', methods=['GET'])
def sync(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        settings = Setting.query.filter_by(account_id=account.id).first_or_404()

        process_sqs_messages(settings)

        return jsonify({"message": "Sync completed successfully"}), 200
    except Exception as e:
        logging.error(f"Error during '/sync' endpoint: {e}")
        return jsonify({"error": "There was an issue processing the sync request."}), 500

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
    try:
        print("Starting plots route")
        account = Account.query.filter_by(url=account_url).first_or_404()
        account.count_page_loads += 1
        settings = Setting.query.filter_by(account_id=account.id).first_or_404()
        
        sources = Source.query.filter_by(account_id=account.id).all()
        print(f"Found {len(sources)} sources")
        
        plot_data = []
        
        for source in sources:
            # Download source data once per source
            csv_content = download_source_file(settings, source)
            if not csv_content:
                flash(f'No data available for source: {source.name}', 'error')
                continue
                
            for plot in source.plots:
                print(f"Processing plot: {plot.id} (type={plot.type})")
                
                try:
                    if plot.type == "metric":
                        data = process_metric_plot(plot, csv_content)
                    else:  # timeline type
                        data = process_timeseries_plot(plot, csv_content)
                    
                    if data.get('error'):
                        print(f"Plot error: {data['error']}")
                        flash(data['error'], 'error')
                        continue
                    
                    config = json.loads(plot.config) if plot.config else {}
                    plot_info = {
                        'plot_id': plot.id,
                        'name': plot.name or "Unnamed Plot",
                        'type': plot.type,
                        'source_name': source.name,
                        'config': config,
                        **data  # Directly merge the data dictionary
                    }
                    
                    print(f"Plot {plot.id} info keys: {plot_info.keys()}")
                    plot_data.append(plot_info)
                    
                except Exception as e:
                    print(f"Error processing plot {plot.id}: {str(e)}")
                    continue

        # Final validation of plot_data
        validated_plot_data = []
        for plot in plot_data:
            try:
                # Test if the plot data can be serialized
                json.dumps(plot)
                validated_plot_data.append(plot)
            except TypeError as e:
                print(f"Skipping plot {plot.get('plot_id')}: {str(e)}")
                continue

        logging.info(f"Total plots processed: {len(validated_plot_data)}")
        db.session.commit()
        
        return render_template('plots.html', 
                          account=account, 
                          sources=sources,
                          plot_data=validated_plot_data)
                          
    except Exception as e:
        logging.error(f"Error loading plots for {account_url}: {e}")
        traceback.print_exc()  # Print the full stack trace
        return "There was an issue loading the plots page.", 500

def validate_source_data(data):
    errors = []
    
    # Validate name
    name = data.get('name', '').strip()
    if not name:
        errors.append("Name cannot be empty")
    elif not re.match(r'^[a-zA-Z0-9_-]+$', name):
        errors.append("Name can only contain letters, numbers, hyphens, and underscores")
    
    # Validate file_filter
    file_filter = data.get('file_filter', '*').strip()
    try:
        re.compile(file_filter.replace('*', '.*'))
    except re.error:
        errors.append("File filter must be a valid pattern")
    
    # Validate include_columns
    include_columns = data.get('include_columns', '').strip()
    if not include_columns:
        errors.append("Include columns must specify a list of column names")
    else:
        columns = [col.strip() for col in include_columns.split(',')]
        if '*' in columns:
            errors.append("Wildcard (*) is not allowed. Please specify exact column names")
        invalid_columns = [col for col in columns if not col]  # Only check for empty columns
        if invalid_columns:
            errors.append("Column names cannot be empty")
    
    # Validate data_points
    try:
        data_points = int(data.get('data_points', 0))
        if not 1 <= data_points <= 1000:
            errors.append("Data points must be between 1 and 1000")
    except ValueError:
        errors.append("Data points must be a valid number")
    
    if errors:
        raise BadRequest(', '.join(errors))
    
    return {
        'name': name,
        'file_filter': file_filter,
        'include_columns': include_columns,
        'data_points': data_points,
        'tail_only': 'tail_only' in data
    }

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
        
        flash('Source created successfully.', 'success')
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

@accounts_bp.route('/<account_url>/source/<int:source_id>/refresh', methods=['POST'])
def refresh_source(account_url, source_id):
    try:
        print(f"Starting refresh for source {source_id} in account {account_url}")
        account = Account.query.filter_by(url=account_url).first_or_404()
        source = Source.query.filter_by(id=source_id, account_id=account.id).first_or_404()
        settings = Setting.query.filter_by(account_id=account.id).first_or_404()
        
        # Reset source status
        source.success = False
        source.error = None
        source.file_id = None  # Also reset the file_id
        db.session.commit()
        
        # Prepare payload for lambda
        payload = {
            'source': {
                'name': source.name,
                'file_filter': source.file_filter,
                'include_columns': source.include_columns,
                'data_points': source.data_points,
                'tail_only': source.tail_only,
                'bucket_name': settings.bucket_name
            }
        }
        
        lambda_url = os.environ.get('LAMBDA_URL')
        if not lambda_url:
            raise ValueError("LAMBDA_URL environment variable not set")
            
        print(f"Sending request to Lambda: {lambda_url}")
        try:
            requests.post(lambda_url, json=payload, timeout=0.1)  # timeout right away
        except requests.exceptions.Timeout:
            # This is expected, ignore it
            pass
        print("Request sent to Lambda")
        
        flash('Source refresh initiated.', 'success')
        
    except Exception as e:
        print(f"Error in refresh_source: {str(e)}")
        logging.error(f"Error refreshing source {source_id}: {e}")
        flash(f'Error refreshing source: {str(e)}', 'error')
    
    return redirect(url_for('accounts.account_plots', account_url=account_url))

@accounts_bp.route('/<account_url>/source/sync', methods=['GET'])
def sync_sources(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        settings = Setting.query.filter_by(account_id=account.id).first_or_404()
        
        # Call the sync function
        sync_source_files(settings)
        
        flash('Source files synced successfully.', 'success')
    except Exception as e:
        logging.error(f"Error syncing source files for account {account_url}: {e}")
        flash('Error syncing source files.', 'error')
    
    return redirect(url_for('accounts.account_plots', account_url=account_url))

@accounts_bp.route('/<account_url>/source/<int:source_id>/plot', methods=['POST'])
def create_plot(account_url, source_id):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        source = Source.query.filter_by(id=source_id, account_id=account.id).first_or_404()
        
        plot_name = request.form.get('name')
        plot_type = request.form.get('type')
        
        if not plot_name or not plot_type:
            flash('Plot name and type are required.', 'error')
            return redirect(url_for('accounts.account_plots', account_url=account_url))
        
        # Build config based on plot type
        config = {}
        if plot_type == 'metric':
            data_column = request.form.get('metric_data_column')
            display_type = request.form.get('display_type')
            
            if not data_column:
                flash('Data column is required for metric plots.', 'error')
                return redirect(url_for('accounts.account_plots', account_url=account_url))
            
            if not display_type or display_type not in ['bar', 'box', 'table']:
                flash('Valid display type (bar, box, or table) is required for metric plots.', 'error')
                return redirect(url_for('accounts.account_plots', account_url=account_url))
                
            config = {
                'data_column': data_column,
                'display': display_type
            }
        else:  # timeline type
            time_column = request.form.get('time_column')
            data_column = request.form.get('data_column')
            
            if not time_column or not data_column:
                flash('Time column and data column are required for timeline plots.', 'error')
                return redirect(url_for('accounts.account_plots', account_url=account_url))
                
            config = {
                'time_column': time_column,
                'data_column': data_column
            }
        
        # Create new plot with JSON config
        plot = Plot(
            source_id=source_id,
            name=plot_name,
            type=plot_type,
            config=json.dumps(config)
        )
        
        db.session.add(plot)
        db.session.commit()
        
        flash('Plot created successfully.', 'success')
    except Exception as e:
        db.session.rollback()
        flash('Error creating plot.', 'error')
        logging.error(f"Error creating plot: {e}")
    
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
        db.session.commit()
        
        flash(f'Plot "{plot_name}" deleted successfully.', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash('Error deleting plot.', 'error')
        logging.error(f"Error deleting plot {plot_id}: {e}")
    
    return redirect(url_for('accounts.account_plots', account_url=account_url))

@accounts_bp.route('/<account_url>/layout-test', methods=['GET'])
def layout_test(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        
        # Get all plots for this account
        plots = []
        for source in Source.query.filter_by(account_id=account.id).all():
            plots.extend(source.plots)
        
        # Get existing layout if available
        layout = Layout.query.filter_by(account_id=account.id).first()
        
        return render_template('layout_test.html',
                             account=account,
                             plots=plots,
                             layout=layout)
    except Exception as e:
        logging.error(f"Error loading layout test for {account_url}: {e}")
        return "There was an issue loading the layout test page.", 500

@accounts_bp.route('/<account_url>/layout', methods=['POST'])
def save_layout(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        data = request.get_json()
        
        # Get or create layout
        layout = Layout.query.filter_by(account_id=account.id).first()
        if not layout:
            layout = Layout(account_id=account.id, name=data['name'])
        
        layout.config = json.dumps(data['config'])
        db.session.add(layout)
        db.session.commit()
        
        return jsonify({'success': True})
    except Exception as e:
        logging.error(f"Error saving layout for {account_url}: {e}")
        return jsonify({'success': False, 'error': str(e)})

@accounts_bp.route('/<account_url>/layout/<int:layout_id>', methods=['GET'])
def layout_view(account_url, layout_id):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        layout = Layout.query.filter_by(id=layout_id, account_id=account.id).first_or_404()
        
        # Get all plots for this account
        plots = []
        plot_data = []
        for source in Source.query.filter_by(account_id=account.id).all():
            # Download source data once per source
            csv_content = download_source_file(account.settings, source)
            if not csv_content:
                continue
                
            for plot in source.plots:
                plots.append(plot)  # For the plot selector
                try:
                    if plot.type == "metric":
                        data = process_metric_plot(plot, csv_content)
                    else:  # timeline type
                        data = process_timeseries_plot(plot, csv_content)
                    
                    if data.get('error'):
                        continue
                    
                    config = json.loads(plot.config) if plot.config else {}
                    plot_info = {
                        'plot_id': plot.id,
                        'name': plot.name,
                        'type': plot.type,
                        'source_name': source.name,
                        'config': config,
                        **data
                    }
                    plot_data.append(plot_info)
                    
                except Exception as e:
                    logging.error(f"Error processing plot {plot.id}: {str(e)}")
                    continue
        
        return render_template('layout_view.html',
                           account=account,
                           layout=layout,
                           plots=plots,
                           plot_data=plot_data)
                           
    except Exception as e:
        logging.error(f"Error loading layout view for {account_url}: {e}")
        traceback.print_exc()
        return "There was an issue loading the layout view page.", 500