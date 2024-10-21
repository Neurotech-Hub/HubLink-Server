from flask import Flask, redirect, render_template, jsonify, request, url_for, flash
# from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from models import db, Account, Setting
from S3Manager import *
import os
import logging
import random
import string

# Configure logging
logging.basicConfig(level=logging.DEBUG)

# Create Flask app with instance folder configuration
app = Flask(__name__, instance_relative_config=True)
app.config['SECRET_KEY'] = os.urandom(24)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///instance/accounts.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Ensure the instance folder exists
if not os.path.exists(app.instance_path):
    os.makedirs(app.instance_path)

# Initialize SQLAlchemy
db.init_app(app)

# Initialize Flask-Migrate
migrate = Migrate(app, db)

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

# Reserved keywords to avoid conflicts
RESERVED_KEYWORDS = ['new', 'docs', 'settings', 'delete', 'data']

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

    new_account = Account(name=user_name, url=unique_path)

    try:
        db.session.add(new_account)
        db.session.flush()  # Flush to get the new account ID without committing

        # Create default settings for the new account
        create_default_settings(new_account.id)

        # Commit the changes for both the account and settings
        db.session.commit()

        logging.debug(f"New account created: {new_account} with URL: {new_account.url}")
        return redirect(url_for('account_dashboard', account_url=new_account.url))
    except Exception as e:
        db.session.rollback()  # Roll back the transaction on error
        logging.error(f"There was an issue adding your account: {e}")
        return "There was an issue adding your account."

# Route to output account settings as JSON
@app.route('/<account_url>.json', methods=['GET'])
def account_settings_json(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        setting = Setting.query.filter_by(account_id=account.id).first()
        if setting:
            return jsonify(setting.to_dict())
        else:
            return jsonify({'error': 'Settings not found'}), 404
    except Exception as e:
        logging.error(f"Error generating JSON for {account_url}: {e}")
        return "There was an issue generating the JSON.", 500

# Route to view the settings for an account
@app.route('/<account_url>/settings', methods=['GET'])
def account_settings(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        settings = Setting.query.filter_by(account_id=account.id).first_or_404()
        return render_template('settings.html', account=account, settings=settings)
    except Exception as e:
        logging.error(f"Error loading settings for {account_url}: {e}")
        return "There was an issue loading the settings page.", 500

# Route to submit updated settings for an account
@app.route('/<account_url>/settings/update', methods=['POST'])
def update_settings(account_url):
    account = Account.query.filter_by(url=account_url).first_or_404()
    settings = Setting.query.filter_by(account_id=account.id).first_or_404()
    try:
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
        settings.id_file_starts_with = request.form['id_file_starts_with']
        settings.alert_email = request.form['alert_email']

        db.session.commit()
        flash("Settings updated successfully.", "success")
        logging.debug(f"Settings updated for account URL {account_url}")
        return redirect(url_for('account_settings', account_url=account_url))
    except Exception as e:
        db.session.rollback()
        flash("There was an issue updating the settings.", "error")
        logging.error(f"There was an issue updating the settings: {e}")
        return "There was an issue updating the settings."

# Route to delete an account
@app.route('/<account_url>/delete', methods=['POST'])
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

# Route to view data for an account
@app.route('/<account_url>/data', methods=['GET'])
def account_data(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        settings = Setting.query.filter_by(account_id=account.id).first_or_404()
        if settings.bucket_name and settings.aws_access_key_id and settings.aws_secret_access_key:
            recent_files = get_recent_files(settings.bucket_name, settings.aws_access_key_id, settings.aws_secret_access_key)
            logging.debug(f"Recent files retrieved: {recent_files}")
        else:
            recent_files = []
        return render_template('data.html', account=account, recent_files=recent_files)
    except Exception as e:
        logging.error(f"Error loading data for {account_url}: {e}")
        return "There was an issue loading the data page.", 500

# Route to view documentation
@app.route('/docs', methods=['GET'])
def docs():
    try:
        return render_template('docs.html')
    except Exception as e:
        logging.error(f"Error loading documentation: {e}")
        return "There was an issue loading the documentation.", 500

# Route to view the account dashboard by its unique URL
@app.route('/<account_url>', methods=['GET'])
def account_dashboard(account_url):
    if account_url in RESERVED_KEYWORDS:
        return page_not_found(404)
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        settings = Setting.query.filter_by(account_id=account.id).first_or_404()
        
        return render_template('dashboard.html', account=account, settings=settings, account_url=account_url)
    except Exception as e:
        logging.error(f"Error loading dashboard for {account_url}: {e}")
        return "There was an issue loading the dashboard.", 500

# Define error handler
@app.errorhandler(404)
def page_not_found(e):
    logging.error(f"404 error: {e}")
    return render_template('404.html'), 404

if __name__ == '__main__':
    app.run(debug=True)