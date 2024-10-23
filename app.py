from flask import Flask, g, redirect, render_template, jsonify, request, url_for
from flask_migrate import Migrate
from models import db, Account, Setting, File
from S3Manager import *
import os
import logging
import random
import string
from datetime import datetime, timedelta, timezone  # Added timezone import
from accounts import accounts_bp  # Importing Blueprint for account-specific routes

# Configure logging
logging.basicConfig(level=logging.DEBUG)
# Suppress botocore and boto3 debugging logs
logging.getLogger('botocore').setLevel(logging.WARNING)
logging.getLogger('boto3').setLevel(logging.WARNING)

# Create Flask app with instance folder configuration
app = Flask(__name__, instance_relative_config=True)
app.config['SECRET_KEY'] = os.urandom(24)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv(
    'DATABASE_URL', 
    f'sqlite:///{os.path.abspath(os.path.join(app.instance_path, "accounts.db"))}'
)

logging.info(f"SQLALCHEMY_DATABASE_URI is set to: {app.config['SQLALCHEMY_DATABASE_URI']}")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize SQLAlchemy
db.init_app(app)

# Initialize Flask-Migrate
migrate = Migrate(app, db)

# Register the Blueprint for account-specific routes
app.register_blueprint(accounts_bp)

# Function to generate random URL strings
def generate_random_string(length=24):
    characters = string.ascii_letters + string.digits
    return ''.join(random.choices(characters, k=length))

# Function to create default settings for a new account
def create_default_settings(account_id):
    try:
        new_setting = Setting(
            aws_access_key_id='',
            aws_secret_access_key='',
            account_id=account_id,
            bucket_name='',
            dt_rule='days',
            max_file_size=5000000,
            use_cloud=False,
            delete_scans=True,
            delete_scans_days_old=-1,
            delete_scans_percent_remaining=-1,
            device_name_includes='ESP32',
            id_file_starts_with='id_',
            alert_email=''
        )
        db.session.add(new_setting)
        db.session.commit()
        logging.debug(f"Default settings created for account ID {account_id}")
    except Exception as e:
        logging.error(f"There was an issue creating default settings for account ID {account_id}: {e}")
        db.session.rollback()

# Route to display the homepage
@app.route('/')
def index():
    try:
        logging.debug("Accessing the index route.")
        all_accounts = Account.query.all()
        logging.debug(f"Accounts retrieved: {all_accounts}")
        return render_template('index.html', accounts=all_accounts)
    except Exception as e:
        logging.error(f"Error loading index: {e}")
        return "There was an issue loading the homepage.", 500
    
# Route to submit a new account
@app.route('/new', methods=['POST'])
def submit():
    user_name = request.form['name']
    unique_path = generate_random_string()

    # Check if user_name contains '|', indicating a specified unique_path
    if "|" in user_name:
        parts = user_name.split("|", 1)
        if len(parts) == 2:
            user_name, unique_path = parts[0], parts[1]

    new_account = Account(name=user_name, url=unique_path)

    try:
        db.session.add(new_account)
        db.session.flush()  # Flush to get the new account ID without committing

        # Create default settings for the new account
        create_default_settings(new_account.id)

        # Commit the changes for both the account and settings
        db.session.commit()

        logging.debug(f"New account created: {new_account} with URL: {new_account.url}")
        return redirect(url_for('accounts.account_dashboard', account_url=new_account.url))  # Updated for Blueprint
    except Exception as e:
        db.session.rollback()  # Roll back the transaction on error
        logging.error(f"There was an issue adding your account: {e}")
        return "There was an issue adding your account."

# Route to view documentation
@app.route('/docs', methods=['GET'])
def docs():
    g.title = "Docs"
    try:
        return render_template('docs.html')
    except Exception as e:
        logging.error(f"Error loading documentation: {e}")
        return "There was an issue loading the documentation.", 500

# Lambda S3 endpoint
@app.route('/force_sync', methods=['GET'])
def force_sync():
    try:
        # Calculate the time window of the last 10 minutes
        ten_minutes_ago = datetime.now(timezone.utc) - timedelta(minutes=10)
        
        # Query accounts that were updated in the last 10 minutes
        accounts_to_sync = Account.query.filter(Account.updated_at >= ten_minutes_ago).all()

        for account in accounts_to_sync:
            # Construct account settings
            account_settings = Setting.query.filter_by(account_id=account.id).first()
            if account_settings:
                # Call update_S3_files for the account with force_update set to True
                update_S3_files(account_settings, force_update=True)

        return jsonify({"message": "Force sync completed successfully"}), 200
    except Exception as e:
        logging.error(f"Error during '/force_sync' endpoint: {e}")
        return jsonify({"error": "There was an issue processing the force sync request."}), 500
    
# Define error handler
@app.errorhandler(404)
def page_not_found(e):
    logging.error(f"404 error: {e}")
    return render_template('404.html'), 404

if __name__ == '__main__':
    app.run(debug=True)
