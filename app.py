from flask import Flask, g, redirect, render_template, jsonify, request, url_for, session, flash
from flask_migrate import Migrate, upgrade
from models import db, Account, Setting, File, Gateway, Source # db locations
from S3Manager import generate_s3_url, setup_aws_resources, cleanup_aws_resources
import os
import logging
import random
import string
from datetime import datetime, timezone, timedelta  # Added timezone import and timedelta
from accounts import accounts_bp  # Importing Blueprint for account-specific routes
from dotenv import load_dotenv
from flask_moment import Moment
import json
from plot_utils import get_plot_data
from sqlalchemy import text
from functools import wraps
from utils import admin_required, get_analytics, initiate_source_refresh

load_dotenv(override=True)

# Configure logging for production environment
formatter = logging.Formatter('%(asctime)s [%(levelname)s] [%(name)s] %(message)s')
handler = logging.StreamHandler()
handler.setFormatter(formatter)

# Configure root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
# Remove any existing handlers to avoid duplicate logs
for h in root_logger.handlers:
    root_logger.removeHandler(h)
root_logger.addHandler(handler)

# Create logger for the application
logger = logging.getLogger(__name__)

# Configure all loggers to propagate and set levels
loggers = ['plot_utils', 'accounts', 'models', 'S3Manager']
for logger_name in loggers:
    module_logger = logging.getLogger(logger_name)
    module_logger.setLevel(logging.INFO)
    # Remove any existing handlers to avoid duplicate logs
    for h in module_logger.handlers:
        module_logger.removeHandler(h)
    module_logger.addHandler(handler)
    module_logger.propagate = False  # Prevent duplicate logs

# Configure SQLAlchemy logging to be less verbose
logging.getLogger('sqlalchemy.engine').setLevel(logging.WARNING)
logging.getLogger('sqlalchemy.pool').setLevel(logging.WARNING)

# Test log message
logger.info("Logging configuration completed")

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
            max_file_size=1073741824,
            use_cloud=False,
            device_name_includes='HUBLINK',
            alert_email='',
            gateway_manages_memory=True
        )
        db.session.add(new_setting)
        db.session.commit()
        logger.debug(f"Default settings created for account ID {account_id}")
    except Exception as e:
        logger.error(f"There was an issue creating default settings for account ID {account_id}: {e}")
        db.session.rollback()

# Add this before the routes
@app.before_request
def load_user():
    g.user = None
    if 'admin_id' in session:
        account = db.session.get(Account, session['admin_id'])
        if account and account.is_admin:
            g.user = account

# Route to display the homepage
@app.route('/')
def index():
    try:
        logger.debug("Accessing the index route.")
        return render_template('index.html')
    except Exception as e:
        logger.error(f"Error loading index: {e}")
        return "There was an issue loading the homepage.", 500

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_id' not in session:
            return redirect(url_for('admin'))
        
        account = db.session.get(Account, session['admin_id'])
        if not account or not account.is_admin:
            session.pop('admin_id', None)
            return redirect(url_for('admin'))
            
        return f(*args, **kwargs)
    return decorated_function

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    g.title = "Admin"
    # Check if already logged in first
    if 'admin_id' in session:
        account = db.session.get(Account, session['admin_id'])
        if account and account.is_admin:
            # Handle GET request - show dashboard
            if request.method == 'GET':
                try:
                    all_accounts = Account.query.all()
                    analytics = get_analytics()  # Get analytics for all accounts
                    
                    if not analytics:
                        flash('Error loading analytics', 'error')
                        analytics = {}
                    
                    return render_template('admin.html', 
                                         accounts=all_accounts,
                                         admin_route='admin',
                                         analytics=analytics)
                except Exception as e:
                    logger.error(f"Error loading admin dashboard: {e}")
                    return "There was an issue loading the page.", 500
            
            # Handle POST request - create new account
            else:
                return submit()

    # Auto-login for localhost only if not already logged in
    if request.remote_addr in ['127.0.0.1', 'localhost'] and 'admin_id' not in session:
        admin_account = Account.query.filter_by(is_admin=True).first()
        if admin_account:
            session['admin_id'] = admin_account.id
            return redirect(url_for('admin'))
    
    # If not logged in or not admin, handle login
    if request.method == 'POST':
        name = request.form.get('name')
        password = request.form.get('password')
        
        account = Account.query.filter_by(name=name).first()
        
        if account and account.is_admin and account.check_password(password):
            session['admin_id'] = account.id
            return redirect(url_for('admin'))
        else:
            flash('Invalid credentials', 'danger')
    
    # Show login page
    return render_template('admin_login.html')

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_id', None)
    flash('Successfully logged out', 'success')
    return redirect(url_for('admin'))

# Route to submit a new account
@app.route('/admin', methods=['POST'])
def submit():
    if 'admin_id' not in session:
        flash('Admin access required', 'danger')
        return redirect(url_for('admin'))

    user_name = request.form.get('name')
    aws_user_name = request.form.get('aws_user_name')
    bucket_name = request.form.get('bucket_name')
    unique_path = generate_random_string()

    # If AWS credentials are requested
    if aws_user_name and bucket_name:
        # Get admin account's settings for AWS operations
        admin_account = Account.query.filter_by(is_admin=True).first()
        logger.info(f"Found admin account: {admin_account.name if admin_account else 'None'}")
        logger.info(f"Admin has AWS credentials: {bool(admin_account and admin_account.settings and admin_account.settings.aws_access_key_id)}")
        
        if not admin_account or not admin_account.settings:
            flash('Error: Admin AWS credentials are not configured. Please set up AWS credentials for the admin account first.', 'danger')
            return redirect(url_for('admin'))

        if not admin_account.settings.aws_access_key_id or not admin_account.settings.aws_secret_access_key:
            flash('Error: Admin AWS credentials are incomplete. Both Access Key ID and Secret Access Key are required.', 'danger')
            return redirect(url_for('admin'))

        # Setup AWS resources
        success, credentials, error = setup_aws_resources(
            admin_account.settings,
            bucket_name,
            aws_user_name
        )

        if not success:
            flash(f'AWS Setup Error: {error}', 'danger')
            if 'AccessDenied' in str(error):
                flash('Hint: The admin account may need additional AWS permissions. Check the documentation for required permissions.', 'warning')
            return redirect(url_for('admin'))

        try:
            new_account = Account(name=user_name, url=unique_path)
            db.session.add(new_account)
            db.session.flush()  # Get the new account ID

            # Create default settings first
            create_default_settings(new_account.id)
            
            # Then update with AWS credentials
            settings = Setting.query.filter_by(account_id=new_account.id).first()
            settings.aws_access_key_id = credentials['aws_access_key_id']
            settings.aws_secret_access_key = credentials['aws_secret_access_key']
            settings.bucket_name = credentials['bucket_name']
            settings.use_cloud = True
            
            db.session.commit()
            
            logger.info(f"New account created with AWS resources: {new_account.name} (ID: {new_account.id})")
            flash('Account created successfully with AWS configuration', 'success')
            return redirect(url_for('accounts.account_dashboard', account_url=new_account.url))

        except Exception as e:
            db.session.rollback()
            logger.error(f"Database error creating account: {e}")
            flash('Failed to create account in database', 'danger')
            return redirect(url_for('admin'))

    else:
        # Create account without AWS credentials
        try:
            new_account = Account(name=user_name, url=unique_path)
            db.session.add(new_account)
            db.session.flush()
            create_default_settings(new_account.id)
            db.session.commit()
            
            logger.debug(f"New account created without AWS: {new_account.name} (ID: {new_account.id})")
            flash('Account created successfully', 'success')
            return redirect(url_for('accounts.account_dashboard', account_url=new_account.url))
        except Exception as e:
            db.session.rollback()
            logger.error(f"There was an issue adding your account: {e}")
            flash('Failed to create account', 'danger')
            return redirect(url_for('admin'))

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
    
@app.route('/cronjob')
def cronjob():
    try:
        # Get SOURCE_INTERVAL_MINUTES from environment, default to 5
        interval_minutes = int(os.getenv('SOURCE_INTERVAL_MINUTES', '5'))
        current_time = datetime.now(timezone.utc)
        
        # Find sources that need updating
        sources = Source.query.filter(
            Source.do_update == True,
            (Source.last_updated == None) | 
            (Source.last_updated <= current_time - timedelta(minutes=interval_minutes))
        ).all()
        
        updated_count = 0
        for source in sources:
            # Get the account for this source
            account = Account.query.get(source.account_id)
            if account:
                success, _ = initiate_source_refresh(account, source)
                if success:
                    updated_count += 1
        
        return jsonify({
            'success': True,
            'message': f'Processed {len(sources)} sources, updated {updated_count}',
            'processed': len(sources),
            'updated': updated_count
        })
        
    except Exception as e:
        logger.error(f"Error in cronjob: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
    
# Define error handler
@app.errorhandler(404)
def page_not_found(e):
    logger.error(f"404 error: {e}")
    return render_template('404.html'), 404

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

@app.route('/admin/account/<int:account_id>/edit', methods=['POST'])
@admin_required
def edit_account(account_id):
    try:
        account = db.session.get(Account, account_id)
        
        # Update basic fields
        account.name = request.form.get('name', account.name).strip()
        account.url = request.form.get('url', account.url).strip()
        account.is_admin = request.form.get('is_admin') == 'on'
        account.use_password = request.form.get('use_password') == 'on'
        
        # Handle password update
        new_password = request.form.get('password', '').strip()
        if new_password or (account.use_password and not account.password_hash):
            account.set_password(new_password)
        elif not account.use_password:
            account.password_hash = None
            
        db.session.commit()
        flash('Account updated successfully', 'success')
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating account {account_id}: {e}")
        flash('Error updating account', 'error')
        
    return redirect(url_for('admin'))

# Route to delete an account
@app.route('/<account_url>/delete', methods=['POST'])
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

@app.route('/admin/account/<int:account_id>/reset-stats', methods=['POST'])
@admin_required
def reset_account_stats(account_id):
    try:
        account = db.session.get(Account, account_id)
        if not account:
            flash('Account not found', 'error')
            return redirect(url_for('admin'))
            
        # Reset all statistics
        account.count_gateway_pings = 0
        account.count_uploaded_files = 0
        account.count_page_loads = 0
        account.count_file_downloads = 0
        account.count_settings_updated = 0
        
        # Delete all gateway entries
        Gateway.query.filter_by(account_id=account_id).delete()
        
        db.session.commit()
        flash('Account statistics reset successfully', 'success')
        
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error resetting stats for account {account_id}: {e}")
        flash('Error resetting account statistics', 'error')
        
    return redirect(url_for('admin'))

if __name__ == '__main__':
    app.run(debug=True)
