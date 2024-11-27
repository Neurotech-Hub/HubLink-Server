from flask import Flask, g, redirect, render_template, jsonify, request, url_for
from flask_migrate import Migrate, upgrade
from models import db, Account, Setting, File, Gateway # db locations
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

# timezone handling
moment = Moment(app)

# security by obscurity
admin_route = os.getenv('ADMIN_ROUTE', 'admin')

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
            alert_email='',
            node_payload=''
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
@app.route(f'/{admin_route}', methods=['GET'])
def add_route_handler():
    try:
        all_accounts = Account.query.all()
        
        # Count unique gateway names
        total_gateways = db.session.query(Gateway.name).distinct().count()
        
        analytics = {
            'total_accounts': len(all_accounts),
            'total_gateways': total_gateways,  # Now counts unique gateways
            'total_gateway_pings': db.session.query(db.func.sum(Account.count_gateway_pings)).scalar() or 0,
            'total_page_loads': db.session.query(db.func.sum(Account.count_page_loads)).scalar() or 0,
            'active_accounts': db.session.query(Account).filter(Account.count_page_loads > 0).count(),
            'total_file_downloads': db.session.query(db.func.sum(Account.count_file_downloads)).scalar() or 0,
            'total_settings_updated': db.session.query(db.func.sum(Account.count_settings_updated)).scalar() or 0,
            'total_uploaded_files': db.session.query(db.func.sum(Account.count_uploaded_files)).scalar() or 0
        }
        
        return render_template('admin.html', 
                             accounts=all_accounts,
                             admin_route=admin_route,
                             analytics=analytics)
    except Exception as e:
        logging.error(f"Error loading new account page: {e}")
        return "There was an issue loading the page.", 500

# Route to submit a new account
@app.route(f'/{admin_route}', methods=['POST'])
def submit():
    user_name = request.form['name']
    unique_path = generate_random_string()
    new_account = Account(name=user_name, url=unique_path)

    try:
        db.session.add(new_account)
        db.session.flush()
        create_default_settings(new_account.id)
        db.session.commit()
        
        logging.debug(f"New account created: {new_account} with URL: {new_account.url}")
        return redirect(url_for('accounts.account_dashboard', account_url=new_account.url))
    except Exception as e:
        db.session.rollback()
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
