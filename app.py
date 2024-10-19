from flask import Flask, redirect, render_template, jsonify, request, url_for
from models import db, Account, Setting
import os
import logging
import random
import string
import boto3

# Configure logging
logging.basicConfig(level=logging.DEBUG)

# Create Flask app with instance folder configuration
app = Flask(__name__, instance_relative_config=True)

# Ensure the instance folder exists
if not os.path.exists(app.instance_path):
    os.makedirs(app.instance_path)

# Configure the SQLite database to be inside the instance folder
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(app.instance_path, 'accounts.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize the database
db.init_app(app)

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

def get_recent_files(bucket_name, aws_access_key_id, aws_secret_access_key, retries=3):
    try:
        # Create S3 client using the access key provided by the user
        s3_client = boto3.client(
            's3',
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            region_name=os.getenv('AWS_REGION', 'us-east-1')  # Default to 'us-east-1' if not set
        )
    except Exception as e:
        logging.error(f"Failed to create S3 client: {e}")
        return []

    for attempt in range(retries):
        try:
            response = s3_client.list_objects_v2(Bucket=bucket_name, MaxKeys=30, Prefix='')
            files = []
            if 'Contents' in response:
                for obj in response['Contents']:
                    pre_signed_url = s3_client.generate_presigned_url(
                        'get_object',
                        Params={'Bucket': bucket_name, 'Key': obj['Key']},
                        ExpiresIn=3600  # URL valid for 1 hour
                    )
                    files.append({
                        'name': obj['Key'],
                        'url': pre_signed_url,
                        'size': obj['Size'],
                        'updated_at': obj['LastModified'].isoformat()
                    })
            return files
        except s3_client.exceptions.NoSuchBucket:
            logging.error(f"Bucket '{bucket_name}' does not exist.")
            return []
        except s3_client.exceptions.ClientError as e:
            error_code = e.response['Error']['Code']
            logging.error(f"Client error ({error_code}) accessing S3 bucket: {e}")
            if error_code == 'AccessDenied':
                logging.error("Access to the bucket was denied.")
                return []
        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed accessing S3 bucket: {e}")
            if attempt < retries - 1:
                logging.debug("Retrying...")
            else:
                return []
    return []

# Route to display the form and list all accounts
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
@app.route('/submit', methods=['POST'])
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

# Route to view the account dashboard by its unique URL
@app.route('/dashboard/<account_url>', methods=['GET'])
@app.route('/dashboard/<account_url>', methods=['GET'])
def account_dashboard(account_url):
    try:
        account = Account.query.filter_by(url=account_url).first_or_404()
        settings = Setting.query.filter_by(account_id=account.id).first_or_404()
        
        # Fetch recent files using the user's S3 API key
        aws_access_key_id = settings.aws_access_key_id
        aws_secret_access_key = settings.aws_secret_access_key
        recent_files = get_recent_files(settings.bucket_name, aws_access_key_id, aws_secret_access_key) if settings.bucket_name else []
        
        return render_template('dashboard.html', account=account, settings=settings, recent_files=recent_files)
    except Exception as e:
        logging.error(f"Error loading dashboard for {account_url}: {e}")
        return "There was an issue loading the dashboard.", 500


# Route to update settings for an account
@app.route('/update/<int:account_id>', methods=['POST'])
def update_settings(account_id):
    settings = Setting.query.filter_by(account_id=account_id).first_or_404()
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
        logging.debug(f"Settings updated for account ID {account_id}")
        return redirect(url_for('account_dashboard', account_url=Account.query.get_or_404(account_id).url))
    except Exception as e:
        db.session.rollback()
        logging.error(f"There was an issue updating the settings: {e}")
        return "There was an issue updating the settings."

# Route to delete an account
@app.route('/delete/<int:account_id>', methods=['POST'])
def delete_account(account_id):
    account_to_delete = Account.query.get_or_404(account_id)
    settings_to_delete = Setting.query.filter_by(account_id=account_id).first()
    try:
        if settings_to_delete:
            db.session.delete(settings_to_delete)
        db.session.delete(account_to_delete)
        db.session.commit()
        logging.debug(f"Account and settings deleted for account ID {account_id}")
        return redirect(url_for('index'))
    except Exception as e:
        db.session.rollback()
        logging.error(f"There was an issue deleting the account: {e}")
        return "There was an issue deleting the account."

# Create tables explicitly before running the app
with app.app_context():
    try:
        db.create_all()  # Create all tables (Account and Setting)
        print("Database tables created successfully.")
    except Exception as e:
        print(f"Error creating tables: {e}")

# Define error handler
@app.errorhandler(404)
def page_not_found(e):
    logging.error(f"404 error: {e}")
    return render_template('404.html'), 404

if __name__ == '__main__':
    app.run(debug=True)
