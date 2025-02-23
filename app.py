from flask import Flask, g, redirect, render_template, jsonify, request, url_for, session, flash
from flask_migrate import Migrate, upgrade
from models import db, Account, Setting, File, Gateway, Source, Admin, Node # db locations
from S3Manager import setup_aws_resources, cleanup_aws_resources, get_storage_usage
import os
import logging
import random
import string
from datetime import datetime, timezone, timedelta
from accounts import accounts_bp  # Importing Blueprint for account-specific routes
from dotenv import load_dotenv
import json
from plot_utils import get_plot_data
from sqlalchemy import text
from sqlalchemy.pool import QueuePool
from functools import wraps
from utils import admin_required, get_analytics, initiate_source_refresh, format_datetime, format_file_size, format_datetime

load_dotenv(override=True)

# Configure logging for production environment
formatter = logging.Formatter('%(asctime)s [%(levelname)s] [%(name)s] %(message)s')
handler = logging.StreamHandler()
handler.setFormatter(formatter)

# Configure root logger first
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
# Remove any existing handlers to avoid duplicate logs
for h in root_logger.handlers:
    root_logger.removeHandler(h)
root_logger.addHandler(handler)

# Create logger for the application
logger = logging.getLogger(__name__)  # This creates the 'app' logger
logger.setLevel(logging.INFO)
logger.propagate = True  # Ensure propagation to root logger

# Configure all module loggers
loggers = ['plot_utils', 'accounts', 'models', 'S3Manager']  # Removed __name__ since we configured it above
for logger_name in loggers:
    module_logger = logging.getLogger(logger_name)
    module_logger.setLevel(logging.INFO)
    # Remove any existing handlers
    for h in module_logger.handlers:
        module_logger.removeHandler(h)
    module_logger.propagate = True

# Configure SQLAlchemy logging to be less verbose
logging.getLogger('sqlalchemy.engine').setLevel(logging.WARNING)
logging.getLogger('sqlalchemy.pool').setLevel(logging.WARNING)

# Test log message
logger.info("Logging configuration completed")

# Create Flask app with instance folder configuration
app = Flask(__name__, instance_relative_config=True)

# Load environment configuration
app.config['ENVIRONMENT'] = os.environ.get('ENVIRONMENT', 'development')
print(f"ENVIRONMENT: {app.config['ENVIRONMENT']}")

# Configure Flask app logger to use our configuration
app.logger.setLevel(logging.INFO)
# Remove default Flask handlers
app.logger.handlers = []
# Let Flask logger propagate to root
app.logger.propagate = True

# Essential Flask and SQLAlchemy configuration
app.config['SECRET_KEY'] = os.urandom(24)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv(
    'DATABASE_URL', 
    f'sqlite:///{os.path.abspath(os.path.join(app.instance_path, "accounts.db"))}'
)

# Configure SQLAlchemy connection pooling
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'poolclass': QueuePool,
    'pool_size': 2,  # Reduced from 5
    'max_overflow': 3,  # Reduced from 10
    'pool_timeout': 20,  # Reduced from 30
    'pool_recycle': 1800,
    'pool_pre_ping': True
}

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize SQLAlchemy
db.init_app(app)

# Enable SQLite foreign key support
# with app.app_context():
#     if db.engine.url.drivername == 'sqlite':
#         from sqlalchemy import event
#         from sqlalchemy.engine import Engine
#         @event.listens_for(Engine, "connect")
#         def set_sqlite_pragma(dbapi_connection, connection_record):
#             cursor = dbapi_connection.cursor()
#             cursor.execute("PRAGMA foreign_keys=ON")
#             cursor.close()

# Initialize Flask-Migrate
migrate = Migrate(app, db)

def cleanup_alembic_tables():
    """Clean up any temporary tables left behind from failed migrations."""
    try:
        with db.engine.connect() as conn:
            tables = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '_alembic_tmp_%'"))
            for table in tables:
                logger.info(f"Dropping temporary table: {table[0]}")
                conn.execute(text(f"DROP TABLE IF EXISTS {table[0]}"))
    except Exception as e:
        logger.error(f"Error cleaning up temporary tables: {e}")

# Note: Database migrations are now handled during app initialization
with app.app_context():
    try:
        # Clean up any temporary tables first
        cleanup_alembic_tables()
        
        # Run migrations
        logger.info("Running database migrations...")
        try:
            upgrade()
            logger.info("Database migrations completed successfully")
        except Exception as e:
            logger.error(f"Error during migrations: {e}")
            
        # Re-enable foreign keys after migrations
        # with db.engine.connect() as conn:
        #     conn.execute(text("PRAGMA foreign_keys=ON"))
            
        # Initialize any required application state
        pass
    except Exception as e:
        logger.error(f"Error during application initialization: {e}")

# Register the Blueprint for account-specific routes
app.register_blueprint(accounts_bp)

# Add custom filters
@app.template_filter('datetime')
def format_datetime_filter(value, format='relative'):
    """
    Template filter for formatting datetime values.
    Uses the format_datetime utility function with the account's timezone setting.
    """
    if value is None:
        return ''
    
    # Get account timezone from context if available
    account = getattr(g, 'account', None)
    timezone = account.settings.timezone if account and hasattr(account, 'settings') else 'America/Chicago'
    
    return format_datetime(value, timezone, format)

@app.template_filter('filesize')
def format_file_size_filter(size_in_bytes):
    """Template filter wrapper for format_file_size utility function."""
    return format_file_size(size_in_bytes)

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

        # Get versioning settings
        version_files = request.form.get('version_files', 'on') == 'on'
        version_days = int(request.form.get('version_days', '7'))

        # Setup AWS resources
        success, credentials, error = setup_aws_resources(
            admin_account.settings,
            bucket_name,
            aws_user_name,
            version_files=version_files,
            version_days=version_days
        )

        if not success:
            flash(f'AWS Setup Error: {error}', 'danger')
            if 'AccessDenied' in str(error):
                flash('Hint: The admin account may need additional AWS permissions. Check the documentation for required permissions.', 'warning')
            return redirect(url_for('admin'))

        try:
            new_account = Account(
                name=user_name, 
                url=unique_path,
                plan_versioned_backups=version_files,
                plan_version_days=version_days
            )
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
    
# Route to view about page
@app.route('/about', methods=['GET'])
def about():
    g.title = "About"
    try:
        analytics = get_analytics()
        return render_template('about.html', analytics=analytics)
    except Exception as e:
        logger.error(f"Error loading about page: {e}")
        return "There was an issue loading the about page.", 500
    
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
        current_time = datetime.now(timezone.utc)
        logger.info(f"Cronjob running at {current_time}")
        
        # Check for daily tasks
        admin = Admin.query.first()
        if not admin:
            admin = Admin()
            db.session.add(admin)
            db.session.commit()
            
        last_cron = admin.last_daily_cron.replace(tzinfo=timezone.utc) if admin.last_daily_cron else None
        if not last_cron or (
            current_time.year != last_cron.year or 
            current_time.month != last_cron.month or 
            current_time.day != last_cron.day
        ):
            logger.info(f"Running daily tasks at {current_time}")
            # check if sum of file.size > account.storage_current_bytes for each account
            # if so, use S3Manager > get_storage_usage() to update account.storage_current_bytes
            for account in Account.query.all():
                total_size = sum(file.size for file in File.query.filter_by(account_id=account.id).all())
                if total_size > account.storage_current_bytes:
                    storage_usage = get_storage_usage(account.settings)
                    account.storage_current_bytes = storage_usage['current_size']
                    account.storage_versioned_bytes = storage_usage['versioned_size']
                    db.session.commit()

                # Check if it's time for monthly reset
                current_time = current_time.replace(tzinfo=timezone.utc)  # Ensure timezone aware
                plan_anniversary = account.plan_start_date.replace(tzinfo=timezone.utc) - timedelta(days=1)
                new_month = current_time.day == plan_anniversary.day

                if new_month:
                    logger.info(f"Monthly reset for account {account.name} (ID: {account.id})")
                    account.count_uploaded_files_mo = 0
                    db.session.commit()

            # Clean up old gateways that have no node associations
            days_ago = current_time - timedelta(days=7)
            # Find gateways older than days_ago that don't have any nodes
            old_gateways = Gateway.query.filter(
                Gateway.created_at <= days_ago,
                ~Gateway.id.in_(db.session.query(Node.gateway_id))
            ).all()
            
            if old_gateways:
                logger.info(f"Deleting {len(old_gateways)} old gateways with no nodes")
                print(f"/cronjob: Deleting {len(old_gateways)} old gateways with no nodes")
                for gateway in old_gateways:
                    db.session.delete(gateway)
                db.session.commit()

            # Update last cron run time
            admin.last_daily_cron = current_time
            db.session.commit()
        
        updated_count = 0
        sources = []  # Initialize sources list
        if app.config['ENVIRONMENT'] == 'production':
            interval_minutes = int(os.getenv('SOURCE_INTERVAL_MINUTES', '5'))
            cutoff_time = current_time - timedelta(minutes=interval_minutes)
            
            logger.info(f"Looking for sources not updated since {cutoff_time}")
            print(f"/cronjob: Looking for sources not updated since {cutoff_time}")
            
            # Find sources that need updating
            sources = Source.query.filter(
                Source.do_update == True,
                (Source.last_updated == None) | 
                (Source.last_updated <= cutoff_time.replace(tzinfo=None))  # Remove timezone info for comparison
            ).all()
            
            logger.info(f"Found {len(sources)} sources that need updating")
            print(f"/cronjob: Found {len(sources)} sources that need updating")
            
            for source in sources:
                last_updated = source.last_updated.replace(tzinfo=timezone.utc) if source.last_updated else None
                logger.info(f"Source {source.id}: last_updated={last_updated}, "
                        f"needs_update={(last_updated is None) or (last_updated <= cutoff_time)}")
            
            for source in sources:
                # Get the account for this source
                account = db.session.get(Account, source.account_id)
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
        else:
            return jsonify({
                'success': True,
                'message': 'Cronjob completed successfully (non-production environment)',
                'processed': 0,
                'updated': 0
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
        
        # Update plan fields
        account.plan_storage_gb = int(request.form.get('plan_storage_gb', account.plan_storage_gb))
        account.plan_uploads_mo = int(request.form.get('plan_uploads_mo', account.plan_uploads_mo))
        account.plan_versioned_backups = request.form.get('plan_versioned_backups') == 'on'
        account.plan_version_days = int(request.form.get('plan_version_days', account.plan_version_days))
        
        # Update plan start date if provided
        plan_start_date = request.form.get('plan_start_date')
        if plan_start_date:
            try:
                account.plan_start_date = datetime.strptime(plan_start_date, '%Y-%m-%d')
            except ValueError as e:
                logger.error(f"Invalid date format for plan_start_date: {e}")
                flash('Invalid date format for plan start date', 'error')
                return redirect(url_for('admin'))
        
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
        account.count_uploaded_files_mo = 0
        account.count_file_downloads = 0
        
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
