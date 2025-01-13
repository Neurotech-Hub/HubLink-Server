from flask import Flask, g, redirect, render_template, jsonify, request, url_for, session, flash
from flask_migrate import Migrate, upgrade
from models import db, Account, Setting, File, Gateway, Source # db locations
from S3Manager import generate_s3_url, setup_aws_resources, cleanup_aws_resources
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
from functools import wraps

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
            delete_scans_days_old=90,
            delete_scans_percent_remaining=90,
            device_name_includes='HUBLINK',
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
    # Check if user is logged in and is admin
    if 'admin_id' in session:
        account = db.session.get(Account, session['admin_id'])
        if account and account.is_admin:
            # Handle GET request - show dashboard
            if request.method == 'GET':
                g.title = "Admin"
                try:
                    all_accounts = Account.query.all()
                    total_gateways = db.session.query(Gateway.name).distinct().count()
                    
                    analytics = {
                        'total_accounts': len(all_accounts),
                        'total_gateways': total_gateways,
                        'total_gateway_pings': db.session.query(db.func.sum(Account.count_gateway_pings)).scalar() or 0,
                        'total_page_loads': db.session.query(db.func.sum(Account.count_page_loads)).scalar() or 0,
                        'active_accounts': db.session.query(Account).filter(Account.count_page_loads > 0).count(),
                        'total_file_downloads': db.session.query(db.func.sum(Account.count_file_downloads)).scalar() or 0,
                        'total_settings_updated': db.session.query(db.func.sum(Account.count_settings_updated)).scalar() or 0,
                        'total_uploaded_files': db.session.query(db.func.sum(Account.count_uploaded_files)).scalar() or 0
                    }
                    
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

            # Create settings with AWS credentials
            new_settings = Setting(
                account_id=new_account.id,
                aws_access_key_id=credentials['aws_access_key_id'],
                aws_secret_access_key=credentials['aws_secret_access_key'],
                bucket_name=credentials['bucket_name'],
                dt_rule='days',
                max_file_size=5000000,
                use_cloud=True,
                delete_scans=True,
                delete_scans_days_old=90,
                delete_scans_percent_remaining=10,
                device_name_includes='HUBLINK',
                alert_file_starts_with='alert_',
                alert_email='',
                node_payload=''
            )
            db.session.add(new_settings)
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
        
        if not data or 'bucket_name' not in data or 'name' not in data:
            print("Missing required fields in request")
            return jsonify({
                'error': 'Missing required fields (bucket_name or name)',
                'status': 400
            }), 400
        
        # First find the account using the bucket name
        settings = Setting.query.filter_by(bucket_name=data['bucket_name']).first()
        if not settings:
            print(f"No account found for bucket: {data['bucket_name']}")
            return jsonify({
                'error': f"No account found for bucket '{data['bucket_name']}'",
                'status': 404
            }), 404
            
        # Find the source using the account and name
        source = Source.query.filter_by(
            account_id=settings.account_id,
            name=data['name']
        ).first()
        
        if not source:
            print(f"No source found with name: {data['name']}")
            return jsonify({
                'error': f"No source found with name '{data['name']}'",
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

# Add this new route
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

if __name__ == '__main__':
    app.run(debug=True)
