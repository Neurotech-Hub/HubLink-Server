from flask import Flask, g, redirect, render_template, jsonify, request, url_for
from flask_migrate import Migrate, upgrade
from models import db, Account, Setting # db locations
from S3Manager import *
import os
import logging
import random
import string
from datetime import datetime, timedelta, timezone  # Added timezone import
from accounts import accounts_bp  # Importing Blueprint for account-specific routes
from dotenv import load_dotenv
from flask_moment import Moment

load_dotenv(override=True)

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] %(levelname)s in %(module)s: %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()  # This will also show logs in console
    ]
)

# Suppress verbose AWS logs
logging.getLogger('botocore').setLevel(logging.WARNING)
logging.getLogger('boto3').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)  # Also suppress urllib3 logs

# Create Flask app with instance folder configuration
app = Flask(__name__, instance_relative_config=True)
app.config['SECRET_KEY'] = os.urandom(24)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv(
    'DATABASE_URL', 
    f'sqlite:///{os.path.abspath(os.path.join(app.instance_path, "accounts.db"))}'
)

# timezone handling
moment = Moment(app)

# security by obscurity
new_route = os.getenv('NEW_ROUTE', 'new')

logging.info(f"SQLALCHEMY_DATABASE_URI is set to: {app.config['SQLALCHEMY_DATABASE_URI']}")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize SQLAlchemy
db.init_app(app)

# Initialize Flask-Migrate
migrate = Migrate(app, db)

# Run migrations
with app.app_context():
    try:
        # First, try to clean up any leftover temporary tables
        with db.engine.connect() as conn:
            conn.execute(db.text("DROP TABLE IF EXISTS _alembic_tmp_setting"))
            conn.commit()
        
        # Now run the migration
        upgrade()
        logging.info("Database migrations completed successfully")
    except Exception as e:
        logging.error(f"Error running database migrations: {e}")

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
            alert_file_starts_with='alert_',
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
        return render_template('index.html')
    except Exception as e:
        logging.error(f"Error loading index: {e}")
        return "There was an issue loading the homepage.", 500

# Define location dynamically
@app.route(f'/{new_route}', methods=['GET'])
def add_route_handler():
    all_accounts = Account.query.all()
    return render_template('new.html', accounts=all_accounts, new_route=new_route)

# !! need to make new route include new_route
# Route to submit a new account
@app.route(f'/{new_route}', methods=['POST'])
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
    
# Route to view pricing
@app.route('/pricing', methods=['GET'])
def pricing():
    g.title = "Pricing"
    try:
        return render_template('pricing.html')
    except Exception as e:
        logging.error(f"Error loading pricing: {e}")
        return "There was an issue loading the pricing.", 500
    
@app.route('/favicon.ico')
def favicon():
    return redirect(url_for('static', filename='favicon.ico'))
    
# Define error handler
@app.errorhandler(404)
def page_not_found(e):
    logging.error(f"404 error: {e}")
    return render_template('404.html'), 404

if __name__ == '__main__':
    app.run(debug=True)
