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
from sqlalchemy import text
from sqlalchemy.pool import QueuePool
from functools import wraps
from utils import admin_required, get_analytics, initiate_source_refresh, format_datetime, format_file_size, format_datetime

load_dotenv(override=True)

def setup_logging(app):
    """Configure logging for the application"""
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

    # Configure Flask app logger
    app.logger.setLevel(logging.INFO)
    # Remove default Flask handlers
    app.logger.handlers = []
    app.logger.addHandler(handler)
    # Don't propagate to avoid duplicate logs
    app.logger.propagate = False

    # Configure all module loggers
    loggers = ['plot_utils', 'accounts', 'models', 'S3Manager']
    for logger_name in loggers:
        module_logger = logging.getLogger(logger_name)
        module_logger.setLevel(logging.INFO)
        for h in module_logger.handlers:
            module_logger.removeHandler(h)
        module_logger.addHandler(handler)
        module_logger.propagate = False

    # Configure SQLAlchemy logging to be less verbose
    logging.getLogger('sqlalchemy.engine').setLevel(logging.WARNING)
    logging.getLogger('sqlalchemy.pool').setLevel(logging.WARNING)

# Create Flask app with instance folder configuration
app = Flask(__name__, instance_relative_config=True)

# Load environment configuration
app.config['ENVIRONMENT'] = os.environ.get('ENVIRONMENT', 'development')

# Setup logging
setup_logging(app)
app.logger.info("Logging configuration completed")
app.logger.info(f"ENVIRONMENT: {app.config['ENVIRONMENT']}")

# Essential Flask and SQLAlchemy configuration
app.config['SECRET_KEY'] = os.urandom(24)

# Database Configuration
DATABASE_URL = os.getenv('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    # Render uses 'postgres://', but SQLAlchemy needs 'postgresql://'
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL or \
    f'sqlite:///{os.path.abspath(os.path.join(app.instance_path, "accounts.db"))}'

# Configure SQLAlchemy connection pooling based on database type
if DATABASE_URL and ('postgresql://' in DATABASE_URL or 'postgres://' in DATABASE_URL):
    # PostgreSQL-specific configuration - optimized for Render hosting
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_size': 5,          # Slightly increased for better concurrent connections
        'max_overflow': 10,      # Increased to handle more peak load
        'pool_timeout': 30,      # Timeout for getting a connection from the pool
        'pool_recycle': 60,      # Recycle connections more frequently to avoid stale connections
        'pool_pre_ping': True,   # Test connections with a ping before using them
        'connect_args': {
            'connect_timeout': 10,
            'application_name': 'hublink',
            'keepalives': 1,           # Enable TCP keepalives
            'keepalives_idle': 30,     # Seconds before sending keepalive
            'keepalives_interval': 10, # Seconds between keepalives
            'keepalives_count': 5      # Number of keepalives before giving up
        }
    }
else:
    # SQLite-specific configuration
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'poolclass': QueuePool,
        'pool_size': 2,
        'max_overflow': 3,
        'pool_timeout': 20,
        'pool_recycle': 1800,
        'pool_pre_ping': True
    }

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize SQLAlchemy
db.init_app(app)

# Initialize Flask-Migrate
migrate = Migrate(app, db)

def cleanup_alembic_tables():
    """Clean up any temporary tables left behind from failed migrations."""
    try:
        with db.engine.connect() as conn:
            if 'postgresql' in str(db.engine.url):
                # PostgreSQL version
                tables = conn.execute(text(
                    "SELECT tablename FROM pg_tables WHERE tablename LIKE '_alembic_tmp_%'"
                ))
            else:
                # SQLite version
                tables = conn.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '_alembic_tmp_%'"
                ))
            
            for table in tables:
                app.logger.info(f"Dropping temporary table: {table[0]}")
                conn.execute(text(f"DROP TABLE IF EXISTS {table[0]}"))
    except Exception as e:
        app.logger.error(f"Error cleaning up temporary tables: {e}")

# Note: Database migrations are now handled during app initialization
if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
    with app.app_context():
        try:
            # Only run migrations if the directory exists
            migrations_dir = os.path.join(os.path.dirname(__file__), 'migrations')
            if os.path.exists(migrations_dir):
                # Clean up any temporary tables first
                cleanup_alembic_tables()
                
                # Run migrations
                app.logger.info("Running database migrations...")
                try:
                    upgrade()
                    app.logger.info("Database migrations completed successfully")
                except Exception as e:
                    app.logger.error(f"Error during migrations: {e}")
            else:
                app.logger.info("Migrations directory not found. Please run 'flask db init' first.")
                
        except Exception as e:
            app.logger.error(f"Error during application initialization: {e}")

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

@app.template_filter('number_format')
def number_format_filter(value):
    """Template filter to format numbers with commas."""
    try:
        return "{:,}".format(int(value))
    except (ValueError, TypeError):
        return value

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
        app.logger.debug(f"Default settings created for account ID {account_id}")
    except Exception as e:
        app.logger.error(f"There was an issue creating default settings for account ID {account_id}: {e}")
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
        app.logger.debug("Accessing the index route.")
        return render_template('index.html')
    except Exception as e:
        app.logger.error(f"Error loading index: {e}")
        return "There was an issue loading the homepage.", 500

# Route to handle admin login
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    # Check if already logged in first
    if 'admin_id' in session:
        account = db.session.get(Account, session['admin_id'])
        if account and account.is_admin:
            return redirect(url_for('admin'))
    
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

# Route to display admin dashboard
@app.route('/admin')
@admin_required
def admin():
    g.title = "Admin"
    try:
        all_accounts = Account.query.all()
        analytics = get_analytics()  # Get analytics for all accounts
        
        if not analytics:
            flash('Error loading analytics', 'error')
            analytics = {}
        
        return render_template('admin.html', 
                             accounts=all_accounts,
                             analytics=analytics)
    except Exception as e:
        app.logger.error(f"Error loading admin dashboard: {e}")
        return "There was an issue loading the page.", 500

# Route to handle admin logout
@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_id', None)
    flash('Successfully logged out', 'success')
    return redirect(url_for('admin_login'))

# Route to create a new account
@app.route('/admin/account/create', methods=['POST'])
@admin_required
def create_account():
    user_name = request.form.get('name')
    aws_user_name = request.form.get('aws_user_name')
    bucket_name = request.form.get('bucket_name')
    unique_path = generate_random_string()

    # If AWS credentials are requested
    if aws_user_name and bucket_name:
        # Get admin account's settings for AWS operations
        admin_account = Account.query.filter_by(is_admin=True).first()
        app.logger.info(f"Found admin account: {admin_account.name if admin_account else 'None'}")
        app.logger.info(f"Admin has AWS credentials: {bool(admin_account and admin_account.settings and admin_account.settings.aws_access_key_id)}")
        
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
            
            app.logger.info(f"New account created with AWS resources: {new_account.name} (ID: {new_account.id})")
            flash('Account created successfully with AWS configuration', 'success')
            return redirect(url_for('accounts.account_dashboard', account_url=new_account.url))

        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Database error creating account: {e}")
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
            
            app.logger.debug(f"New account created without AWS: {new_account.name} (ID: {new_account.id})")
            flash('Account created successfully', 'success')
            return redirect(url_for('accounts.account_dashboard', account_url=new_account.url))
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"There was an issue adding your account: {e}")
            flash('Failed to create account', 'danger')
            return redirect(url_for('admin'))

# Route to view documentation
@app.route('/docs', methods=['GET'])
def docs():
    g.title = "Docs"
    try:
        return render_template('docs.html')
    except Exception as e:
        app.logger.error(f"Error loading documentation: {e}")
        return "There was an issue loading the documentation.", 500
    
# Route to view about page
@app.route('/about', methods=['GET'])
def about():
    g.title = "About"
    try:
        analytics = get_analytics()
        return render_template('about.html', analytics=analytics)
    except Exception as e:
        app.logger.error(f"Error loading about page: {e}")
        return "There was an issue loading the about page.", 500
    
# Route to view pricing
@app.route('/pricing', methods=['GET'])
def pricing():
    g.title = "Pricing"
    try:
        return render_template('pricing.html')
    except Exception as e:
        app.logger.error(f"Error loading pricing: {e}")
        return "There was an issue loading the pricing.", 500
    
@app.route('/favicon.ico')
def favicon():
    return redirect(url_for('static', filename='favicon.ico'))
    
@app.route('/cronjob')
def cronjob():
    try:
        current_time = datetime.now(timezone.utc)
        app.logger.info(f"Cronjob running at {current_time}")
        
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
            app.logger.info(f"Running daily tasks at {current_time}")
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
                    app.logger.info(f"Monthly reset for account {account.name} (ID: {account.id})")
                    account.count_uploaded_files_mo = 0
                    db.session.commit()

            # Update last cron run time
            admin.last_daily_cron = current_time
            db.session.commit()
        
        # Clean up old gateways that have no node associations (runs on every cronjob)
        days_ago = current_time - timedelta(days=30)
        app.logger.info(f"Checking for gateways older than {days_ago}")
        
        # First, get a count to assess the scope
        total_old_gateways = Gateway.query.filter(
            Gateway.created_at <= days_ago,
            ~Gateway.id.in_(
                db.session.query(Node.gateway_id).distinct()
            )
        ).count()
        
        if total_old_gateways > 0:
            app.logger.info(f"Found {total_old_gateways} old gateways eligible for cleanup")
            print(f"/cronjob: Found {total_old_gateways} old gateways eligible for cleanup")
            
            # Conservative approach: limit to last 1000 records to avoid overwhelming the system
            max_delete_count = 1000
            if total_old_gateways > max_delete_count:
                app.logger.info(f"Limiting cleanup to {max_delete_count} records to avoid system overload")
                print(f"/cronjob: Limiting cleanup to {max_delete_count} records to avoid system overload")
            
            # Get IDs of the oldest records (limit to max_delete_count)
            gateway_ids_to_delete = db.session.query(Gateway.id).filter(
                Gateway.created_at <= days_ago,
                ~Gateway.id.in_(
                    db.session.query(Node.gateway_id).distinct()
                )
            ).order_by(Gateway.created_at.asc()).limit(max_delete_count).all()
            
            app.logger.info(f"Retrieved {len(gateway_ids_to_delete)} gateway IDs for deletion")
            print(f"/cronjob: Retrieved {len(gateway_ids_to_delete)} gateway IDs for deletion")
            
            if gateway_ids_to_delete:
                # Extract IDs from the result tuples
                ids_to_delete = [g[0] for g in gateway_ids_to_delete]
                
                # Delete in efficient batches
                batch_size = 100
                total_deleted = 0
                
                for i in range(0, len(ids_to_delete), batch_size):
                    batch_ids = ids_to_delete[i:i + batch_size]
                    
                    try:
                        deleted_count = Gateway.query.filter(
                            Gateway.id.in_(batch_ids)
                        ).delete(synchronize_session=False)
                        
                        total_deleted += deleted_count
                        db.session.commit()
                        
                        app.logger.info(f"Deleted batch {i//batch_size + 1}: {deleted_count} gateways")
                        print(f"/cronjob: Deleted batch {i//batch_size + 1}: {deleted_count} gateways")
                    except Exception as e:
                        app.logger.error(f"Error in bulk DELETE for batch {i//batch_size + 1}: {e}")
                        print(f"/cronjob: Error in bulk DELETE for batch {i//batch_size + 1}: {e}")
                        db.session.rollback()
                        continue
                
                app.logger.info(f"Total deleted: {total_deleted} old gateways with no nodes")
                print(f"/cronjob: Total deleted: {total_deleted} old gateways with no nodes")

        # [ ] delete nodes > 30 days but always keep one row for each unique node so count is correct
        
        updated_count = 0
        sources = []  # Initialize sources list
        if app.config['ENVIRONMENT'] == 'production':
            interval_minutes = int(os.getenv('SOURCE_INTERVAL_MINUTES', '5'))
            cutoff_time = current_time - timedelta(minutes=interval_minutes)
            
            app.logger.info(f"Looking for sources not updated since {cutoff_time}")
            print(f"/cronjob: Looking for sources not updated since {cutoff_time}")
            
            # Find sources that need updating
            sources = Source.query.filter(
                Source.do_update == True,
                (Source.last_updated == None) | 
                (Source.last_updated <= cutoff_time.replace(tzinfo=None))  # Remove timezone info for comparison
            ).all()
            
            app.logger.info(f"Found {len(sources)} sources that need updating")
            print(f"/cronjob: Found {len(sources)} sources that need updating")
            
            for source in sources:
                last_updated = source.last_updated.replace(tzinfo=timezone.utc) if source.last_updated else None
                app.logger.info(f"Source {source.id}: last_updated={last_updated}, "
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
        app.logger.error(f"Error in cronjob: {e}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
    
# Define error handler
@app.errorhandler(404)
def page_not_found(e):
    app.logger.error(f"404 error: {e}")
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
                app.logger.error(f"Invalid date format for plan_start_date: {e}")
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
        app.logger.error(f"Error updating account {account_id}: {e}")
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
        app.logger.error(f"Error resetting stats for account {account_id}: {e}")
        flash('Error resetting account statistics', 'error')
        
    return redirect(url_for('admin'))

if __name__ == '__main__':
    app.run(debug=True)
