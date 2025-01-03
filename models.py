from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone
import logging
import json
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

# Define the accounts model
class Account(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    url = db.Column(db.String(200), nullable=False, unique=True)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # Add new tracking columns with default value of 0
    count_gateway_pings = db.Column(db.Integer, nullable=False, default=0)
    count_uploaded_files = db.Column(db.Integer, nullable=False, default=0)
    count_page_loads = db.Column(db.Integer, nullable=False, default=0)
    count_file_downloads = db.Column(db.Integer, nullable=False, default=0)
    count_settings_updated = db.Column(db.Integer, nullable=False, default=0)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    password_hash = db.Column(db.String(256), nullable=True)
    use_password = db.Column(db.Boolean, nullable=False, default=False)

    # Define relationship with settings
    settings = db.relationship('Setting', backref='account', uselist=False, 
                             cascade="all, delete-orphan")

    # Define relationship with gateways
    gateways = db.relationship('Gateway', backref='account', 
                             cascade="all, delete-orphan")

    # Define relationship with sources
    sources = db.relationship('Source', backref='account', 
                            cascade="all, delete-orphan")

    # Define relationship with layouts
    layouts = db.relationship('Layout', backref='account', 
                            cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Account {self.name}>"

    def set_password(self, password):
        if password:
            self.password_hash = generate_password_hash(password)
            self.use_password = True
        else:
            self.password_hash = None
            self.use_password = False

    def check_password(self, password):
        if not self.use_password or not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

# Define the settings model
class Setting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    aws_access_key_id = db.Column(db.String(200), nullable=True)
    aws_secret_access_key = db.Column(db.String(200), nullable=True)
    bucket_name = db.Column(db.String(200), nullable=True)
    dt_rule = db.Column(db.String(50), nullable=False)
    max_file_size = db.Column(db.Integer, nullable=False)
    use_cloud = db.Column(db.Boolean, nullable=False)
    delete_scans = db.Column(db.Boolean, nullable=False)
    delete_scans_days_old = db.Column(db.Integer, nullable=True)
    delete_scans_percent_remaining = db.Column(db.Integer, nullable=True)
    device_name_includes = db.Column(db.String(100), nullable=True)
    alert_file_starts_with = db.Column(db.String(100), nullable=False, default="")
    alert_email = db.Column(db.String(100), nullable=True)
    node_payload = db.Column(db.String(100), nullable=False, default="")

    def __repr__(self):
        return f"<Setting for Account {self.account_id}>"

    def to_dict(self):
        return {
            'aws_access_key_id': self.aws_access_key_id,
            'aws_secret_access_key': self.aws_secret_access_key,
            'bucket_name': self.bucket_name,
            'dt_rule': self.dt_rule,
            'max_file_size': self.max_file_size,
            'use_cloud': self.use_cloud,
            'delete_scans': self.delete_scans,
            'delete_scans_days_old': self.delete_scans_days_old,
            'delete_scans_percent_remaining': self.delete_scans_percent_remaining,
            'device_name_includes': self.device_name_includes,
            'alert_file_starts_with': self.alert_file_starts_with,
            'alert_email': self.alert_email,
            'node_payload': self.node_payload
        }

# Define the files model
class File(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    key = db.Column(db.String(200), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    size = db.Column(db.Integer, nullable=False)
    last_modified = db.Column(db.DateTime, nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1)
    last_checked = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f"<File {self.key} for Account {self.account_id}>"

    def to_dict(self):
        return {
            'account_id': self.account_id,
            'key': self.key,
            'url': self.url,
            'size': self.size,
            'last_modified': self.last_modified.isoformat(),
            'version': self.version,
            'last_checked': self.last_checked.isoformat() if self.last_checked else None
        }

# Define the device model with ip_address instead of mac_address
class Gateway(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    ip_address = db.Column(db.String(45), nullable=False)  # IPv4 is 15 characters max, IPv6 is up to 45 characters
    name = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    def __repr__(self):
        return f"<Gateway {self.name} with IP {self.ip_address}>"

    def to_dict(self):
        return {
            'id': self.id,
            'account_id': self.account_id,
            'ip_address': self.ip_address,
            'name': self.name,
            'created_at': self.created_at.isoformat()
        }

# Define the source model
class Source(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    file_filter = db.Column(db.String(200), nullable=False, default="*")
    include_columns = db.Column(db.String(500), nullable=False, default="")
    data_points = db.Column(db.Integer, nullable=False, default=0)
    tail_only = db.Column(db.Boolean, nullable=False, default=False)
    last_updated = db.Column(db.DateTime, nullable=True)
    error = db.Column(db.String(500), nullable=True)
    file_id = db.Column(db.Integer, db.ForeignKey('file.id', name='fk_source_file'), nullable=True)
    devices = db.Column(db.String(500), nullable=False, default="")
    state = db.Column(db.String(50), nullable=False, default='created')
    include_archive = db.Column(db.Boolean, nullable=False, default=False)
    file = db.relationship('File', backref=db.backref('sources', lazy=True))

    def __repr__(self):
        return f"<Source {self.name} for Account {self.account_id}>"

    def to_dict(self):
        """Convert source to dictionary with dynamic state"""
        data = {
            'id': self.id,
            'name': self.name,
            'file_filter': self.file_filter,
            'include_columns': self.include_columns,
            'data_points': self.data_points,
            'tail_only': self.tail_only,
            'last_updated': self.last_updated.isoformat() if self.last_updated else None,
            'state': self.state,
            'error': self.error,
            'file_id': self.file_id,  # Just include the file_id
            'devices': json.loads(self.devices) if self.devices else [],  # Decode JSON string to list
            'include_archive': self.include_archive
        }

        return data

    def get_data(self):
        from S3Manager import download_source_file
        settings = Setting.query.filter_by(account_id=self.account_id).first()
        if not settings:
            return None
        return download_source_file(settings, self)

# Define the plot model
class Plot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey('source.id', ondelete='CASCADE'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(50), nullable=False, default="timeline")
    config = db.Column(db.String(500), nullable=False)  # JSON string
    data = db.Column(db.Text, nullable=True)  # JSON string of processed plot data
    
    # Add relationship to Source
    source = db.relationship('Source', backref=db.backref('plots', lazy=True, cascade="all, delete-orphan"))

    def __repr__(self):
        return f"<Plot {self.name} ({self.type}) for Source {self.source_id}>"

    def to_dict(self):
        return {
            'id': self.id,
            'source_id': self.source_id,
            'name': self.name,
            'type': self.type,
            'config': json.loads(self.config),
            'data': json.loads(self.info) if self.info else None
        }

# Define the layout model
class Layout(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    config = db.Column(db.Text, nullable=False)  # JSON string storing grid layout
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    is_default = db.Column(db.Boolean, nullable=False, default=False)
    show_nav = db.Column(db.Boolean, nullable=False, default=False)
    time_range = db.Column(db.String(20), nullable=False, default="all")  # all, week, month, 90days, year
    
    def __repr__(self):
        return f"<Layout {self.name} for Account {self.account_id}>"

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'config': json.loads(self.config),
            'created_at': self.created_at.isoformat(),
            'is_default': self.is_default,
            'show_nav': self.show_nav
        }
