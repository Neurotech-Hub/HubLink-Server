import string
import random
from flask import render_template, request, redirect, url_for
from models import db, Account, Setting

# Function to generate random URL strings
def generate_random_string(length=16):
    characters = string.ascii_letters + string.digits
    return ''.join(random.choices(characters, k=length))

# Route to display the form and list all accounts
def index():
    all_accounts = Account.query.all()
    return render_template('index.html', accounts=all_accounts)

# Route to submit a new account
def submit():
    user_name = request.form['name']
    unique_path = generate_random_string()

    new_account = Account(name=user_name, url=unique_path)

    try:
        db.session.add(new_account)
        db.session.commit()

        # Create default settings for the new account
        new_setting = Setting(
            account_id=new_account.id,
            bucket_name='neurotechhub-000',
            dt_rule='days',
            max_file_size=5000000,
            use_cloud=False,
            delete_scans=True,
            delete_scans_days_old=-1,
            delete_scans_percent_remaining=-1,
            device_name_includes='ESP32',
            id_file_starts_with='id_'
        )
        db.session.add(new_setting)
        db.session.commit()

        return redirect(url_for('index'))
    except Exception as e:
        return f"There was an issue adding your account: {str(e)}"

# Route to view the account dashboard by its unique URL
def account_dashboard(account_url):
    account = Account.query.filter_by(url=account_url).first_or_404()
    settings = Setting.query.filter_by(account_id=account.id).first()
    return render_template('dashboard.html', account=account, settings=settings)

# Route to update settings for an account
def update_settings(account_id):
    settings = Setting.query.filter_by(account_id=account_id).first_or_404()
    try:
        settings.bucket_name = request.form['bucket_name']
        settings.dt_rule = request.form['dt_rule']
        settings.max_file_size = int(request.form['max_file_size'])
        settings.use_cloud = request.form['use_cloud'] == 'true'
        settings.delete_scans = request.form['delete_scans'] == 'true'
        settings.delete_scans_days_old = int(request.form['delete_scans_days_old']) if request.form['delete_scans_days_old'] else None
        settings.delete_scans_percent_remaining = int(request.form['delete_scans_percent_remaining']) if request.form['delete_scans_percent_remaining'] else None
        settings.device_name_includes = request.form['device_name_includes']
        settings.id_file_starts_with = request.form['id_file_starts_with']

        db.session.commit()
        return redirect(url_for('account_dashboard', account_url=Account.query.get_or_404(account_id).url))
    except Exception as e:
        return f"There was an issue updating the settings: {str(e)}"

# Route to delete an account
def delete_account(account_id):
    account_to_delete = Account.query.get_or_404(account_id)
    settings_to_delete = Setting.query.filter_by(account_id=account_id).first()
    try:
        if settings_to_delete:
            db.session.delete(settings_to_delete)
        db.session.delete(account_to_delete)
        db.session.commit()
        return redirect(url_for('index'))
    except Exception as e:
        return f"There was an issue deleting the account: {str(e)}"
