from flask import Flask, g, redirect, render_template, jsonify, request, url_for
from flask_migrate import Migrate, upgrade
from models import db, Account, Setting, File, Gateway, Source # db locations
from S3Manager import generate_s3_url
import os
import logging
import random
import string
from datetime import datetime, timezone  # Added timezone import
from accounts import accounts_bp  # Importing Blueprint for account-specific routes
from dotenv import load_dotenv
from flask_moment import Moment
import json
from plot_utils import get_plot_data
from sqlalchemy import text

load_dotenv(override=True)

# Configure root logger
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    force=True  # This will override any existing configuration
)

# Create logger for the application
logger = logging.getLogger(__name__)

# Ensure all loggers are set to INFO level
logging.getLogger('plot_utils').setLevel(logging.INFO)
logging.getLogger('accounts').setLevel(logging.INFO)

# Create Flask app with instance folder configuration
app = Flask(__name__, instance_relative_config=True)
app.config['SECRET_KEY'] = os.urandom(24)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv(
    'DATABASE_URL', 
    f'sqlite:///{os.path.abspath(os.path.join(app.instance_path, "accounts.db"))}'
)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# timezone handling
moment = Moment(app)

# security by obscurity
admin_route = os.getenv('ADMIN_ROUTE', 'admin')

# Initialize SQLAlchemy
db.init_app(app)

# Initialize Flask-Migrate
migrate = Migrate(app, db)

# Clean up temporary migration tables and run migrations
with app.app_context():
    try:
        # Clean up any temporary migration tables first
        with db.engine.connect() as conn:
            # Get all table names
            tables = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '_alembic_tmp_%'"))
            # Drop each temporary table
            for table in tables:
                conn.execute(text(f"DROP TABLE IF EXISTS {table[0]}"))
                logger.info(f"Dropped temporary table: {table[0]}")
            conn.commit()
        
        # Only run migrations in production
        if os.getenv('ENVIRONMENT', 'development') == 'production':
            upgrade()
            logger.info("Database migrations completed successfully")
    except Exception as e:
        logger.error(f"Error during database cleanup/migration: {e}")

# Register the Blueprint for account-specific routes
app.register_blueprint(accounts_bp)

# Add custom filters
@app.template_filter('datetime')
def format_datetime(value):
    if value is None:
        return ''
    # Return ISO format for moment.js to parse and format on client side
    return value.isoformat()

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
        logger.debug(f"Default settings created for account ID {account_id}")
    except Exception as e:
        logger.error(f"There was an issue creating default settings for account ID {account_id}: {e}")
        db.session.rollback()

# Route to display the homepage
@app.route('/')
def index():
    try:
        logger.debug("Accessing the index route.")
        return render_template('index.html')
    except Exception as e:
        logger.error(f"Error loading index: {e}")
        return "There was an issue loading the homepage.", 500

# Define location dynamically
@app.route(f'/{admin_route}', methods=['GET'])
def add_route_handler():
    g.title = "Admin"
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
        logger.error(f"Error loading new account page: {e}")
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
        
        logger.debug(f"New account created: {new_account} with URL: {new_account.url}")
        return redirect(url_for('accounts.account_dashboard', account_url=new_account.url))
    except Exception as e:
        db.session.rollback()
        logger.error(f"There was an issue adding your account: {e}")
        return "There was an issue adding your account."

# Route to view documentation
@app.route('/docs', methods=['GET'])
def docs():
    g.title = "Docs"
    try:
        return render_template('docs.html')
    except Exception as e:
        logger.error(f"Error loading documentation: {e}")
        return "There was an issue loading the documentation.", 500
    
# Route to view pricing
@app.route('/pricing', methods=['GET'])
def pricing():
    g.title = "Pricing"
    try:
        return render_template('pricing.html')
    except Exception as e:
        logger.error(f"Error loading pricing: {e}")
        return "There was an issue loading the pricing.", 500
    
@app.route('/favicon.ico')
def favicon():
    return redirect(url_for('static', filename='favicon.ico'))
    
# Define error handler
@app.errorhandler(404)
def page_not_found(e):
    logger.error(f"404 error: {e}")
    return render_template('404.html'), 404

@app.route('/source', methods=['POST'])
def create_source():
    try:
        print("Received source update request")
        data = request.get_json()
        print(f"Request data: {data}")
        
        if not data or 'name' not in data:
            print("Missing required fields in request")
            return jsonify({
                'error': 'Missing required fields',
                'status': 400
            }), 400
        
        # Find the source by name
        source = Source.query.filter_by(name=data['name']).first()
        if not source:
            print(f"Source not found: {data['name']}")
            return jsonify({
                'error': f"Source '{data['name']}' not found",
                'status': 404
            }), 404
        
        print(f"Found source: {source.name} (ID: {source.id})")
        
        # Update source fields
        source.devices = json.dumps(data.get('devices', []))
        is_success = not data.get('error')  # Success if no error field or error is empty
        source.state = 'success' if is_success else 'error'
        source.error = data.get('error')  # Store error message if present
        source.last_updated = datetime.now(timezone.utc)
        
        # Handle file record
        file = File.query.filter_by(account_id=source.account_id, key=data['key']).first()
        if not file:
            print(f"Creating new file record for key: {data['key']}")
            file = File(
                account_id=source.account_id,
                key=data['key'],
                url=generate_s3_url(source.account.settings.bucket_name, data['key']),
                size=data['size'],
                last_modified=datetime.now(timezone.utc),
                version=1
            )
            db.session.add(file)
            db.session.flush()
        else:
            file.last_modified = datetime.now(timezone.utc)
        
        source.file_id = file.id
        
        # If source update was successful (no error), update all associated plots
        if is_success:
            print(f"Updating plots for source {source.id}")
            for plot in source.plots:
                try:
                    plot_data = get_plot_data(plot, source, source.account)
                    plot.data = json.dumps(plot_data)
                    print(f"Successfully updated plot {plot.id}")
                except Exception as e:
                    print(f"Error updating plot {plot.id}: {str(e)}")
                    logger.error(f"Error updating plot {plot.id}: {e}")
                    # Continue processing other plots even if one fails
                    continue
        
        db.session.commit()
        print("Successfully committed all changes to database")
        
        return jsonify({
            'message': 'Source and plots updated successfully',
            'status': 200
        })
        
    except Exception as e:
        print(f"Error in create_source: {str(e)}")
        logger.error(f"Error updating source status: {e}")
        db.session.rollback()
        return jsonify({
            'error': 'Internal server error',
            'status': 500
        }), 500

@app.template_filter('to_csv')
def to_csv_filter(value):
    if not value:
        return ''
    try:
        # If value is a string (JSON), parse it first
        if isinstance(value, str):
            value = json.loads(value)
        # Convert each row to comma-separated string
        return '\n'.join([','.join(map(str, row)) for row in value])
    except Exception as e:
        print(f"Error converting to CSV: {e}")
        return str(value)

@app.template_filter('from_json')
def from_json_filter(value):
    if not value:
        return []
    try:
        if isinstance(value, str):
            return json.loads(value)
        return value
    except Exception as e:
        print(f"Error parsing JSON: {e}")
        return []

if __name__ == '__main__':
    app.run(debug=True)
