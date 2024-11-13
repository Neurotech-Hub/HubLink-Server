from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, g
from models import db, Account, Setting, File, Gateway
from datetime import datetime, timedelta
import logging
from S3Manager import *
import traceback
from werkzeug.exceptions import NotFound
from sqlalchemy import desc

# Create the Blueprint for account-related routes
accounts_bp = Blueprint('accounts', __name__)

# Route to output account settings as JSON
@accounts_bp.route('/<account_url>.json', methods=['GET'])
@accounts_bp.route('/<account_url>.json/<gateway_name>', methods=['GET'])
def get_account(account_url, gateway_name=None):
    g.title = "API"
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
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

# Route to view the settings for an account
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
    settings = Setting.query.filter_by(account_id=account.id).first_or_404()
    try:
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
            rebuild_S3_files(settings, True)  # Call update_S3_files if any of the AWS settings changed

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
    total_limit = 100
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        
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
        # Log the incoming request
        logging.debug(f"Received request for checking files for account: {account_url}")
        
        # Get the account details
        account = Account.query.filter_by(url=account_url).first_or_404()

        # Get the list of filenames and sizes from the request JSON body
        request_data = request.get_json()
        logging.debug(f"Request data: {request_data}")

        if not request_data or 'files' not in request_data:
            return jsonify({"error": "Invalid input. JSON body must contain 'files'."}), 400
        
        files = request_data.get('files', [])
        
        if not isinstance(files, list) or not all(isinstance(file, dict) and 'filename' in file and 'size' in file for file in files):
            logging.error("Invalid input structure for 'files'. Must be a list of dictionaries with 'filename' and 'size'.")
            return jsonify({"error": "Invalid input. 'files' must be a list of dictionaries with 'filename' and 'size' keys."}), 400

        # Delegate to existing function to check file existence
        result = do_files_exist(account.id, files)
        logging.debug(f"File existence result: {result}")

        # Return the results as a list of booleans
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

        # Call update_S3_files for the account with force_update set to True
        rebuild_S3_files(settings)

        return jsonify({"message": "Rebuild completed successfully"}), 200
    except Exception as e:
        logging.error(f"Error during '/rebuild' endpoint: {e}")
        return jsonify({"error": "There was an issue processing the rebuild request."}), 500

# Route to view the account dashboard by its unique URL
@accounts_bp.route('/<account_url>', methods=['GET'])
def account_dashboard(account_url):
    g.title = "Dashboard"
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        settings = Setting.query.filter_by(account_id=account.id).first_or_404()
        gateways = Gateway.query.order_by(desc(Gateway.created_at)).limit(20).all()

        # Use UTC for consistency
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        start_date = today - timedelta(days=30)
        recent_files = get_latest_files(account.id, 1000, 31)

        # Send timestamps to client for conversion
        file_uploads = []
        device_uploads = {}

        for file in recent_files:
            # Store UTC timestamp
            timestamp = file.last_modified.timestamp() * 1000  # Convert to milliseconds for JavaScript
            file_uploads.append(timestamp)

            # Track device uploads
            device = file.key.split('/')[0]
            device_uploads[device] = device_uploads.get(device, 0) + 1

        # Sort devices by number of uploads and take the top 10
        sorted_devices = sorted(device_uploads.items(), key=lambda x: x[1], reverse=True)[:10]
        devices = [device for device, count in sorted_devices]
        device_upload_counts = [count for device, count in sorted_devices]

        alerts = [
            {"device": "Device A", "message": "Error detected", "created_at": "2024-10-21 10:15:00"},
            {"device": "Device B", "message": "Warning issued", "created_at": "2024-10-21 11:30:00"},
            {"device": "Device C", "message": "Low battery", "created_at": "2024-10-21 12:00:00"}
        ]

        return render_template(
            'dashboard.html',
            account=account,
            settings=settings,
            file_uploads=file_uploads,  # Send raw timestamps
            gateways=gateways,
            devices=devices,
            device_upload_counts=device_upload_counts
        )
    except NotFound as e:
        # Handle NotFound exception separately to render the 404 template
        logging.error(f"404 Not Found for account URL: {account_url}, error: {e}")
        return render_template('404.html'), 404

    except Exception as e:
        # Handle other unexpected exceptions
        logging.error(f"Error loading dashboard for {account_url}: {e}")
        return "There was an issue loading the dashboard.", 500

@accounts_bp.route('/<account_url>/download/<int:file_id>', methods=['GET'])
def download_file(account_url, file_id):
    try:
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