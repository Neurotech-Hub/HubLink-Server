from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

# Define the accounts model
class Account(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    url = db.Column(db.String(200), nullable=False, unique=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Account {self.name}>"

# Define the settings model
class Setting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    bucket_name = db.Column(db.String(200), nullable=False)
    dt_rule = db.Column(db.String(50), nullable=False)
    max_file_size = db.Column(db.Integer, nullable=False)
    use_cloud = db.Column(db.Boolean, nullable=False)
    delete_scans = db.Column(db.Boolean, nullable=False)
    delete_scans_days_old = db.Column(db.Integer, nullable=True)
    delete_scans_percent_remaining = db.Column(db.Integer, nullable=True)
    device_name_includes = db.Column(db.String(100), nullable=True)
    id_file_starts_with = db.Column(db.String(100), nullable=True)

    def __repr__(self):
        return f"<Setting for Account {self.account_id}>"
