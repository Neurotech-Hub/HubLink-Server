from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone
import logging
import json
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.sql import func

logger = logging.getLogger(__name__)

db = SQLAlchemy()

logger.info("Models module initialized")

# Define the accounts model
class Account(db.Model):
    __tablename__ = 'account'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, server_default='')
    url = db.Column(db.String(200), nullable=False, unique=True, server_default='')
    updated_at = db.Column(db.DateTime, server_default=func.now(), onupdate=func.now())

    # Plan and storage tracking fields
    plan_uploads_mo = db.Column(db.Integer, nullable=False, server_default='500')
    plan_storage_gb = db.Column(db.Integer, nullable=False, server_default='10')
    plan_versioned_backups = db.Column(db.Boolean, nullable=False, server_default='1')
    plan_version_days = db.Column(db.Integer, nullable=False, server_default='7')
    plan_start_date = db.Column(db.DateTime, nullable=False, server_default=func.now())
    # we should set storage_current_bytes using S3Manager.get_storage_usage()
    # we can then compare it to sum(file.size) to determine if we need to re-run
    storage_current_bytes = db.Column(db.Integer, nullable=False, server_default='0')
    storage_versioned_bytes = db.Column(db.Integer, nullable=False, server_default='0')

    # Add new tracking columns with default value of 0
    count_gateway_pings = db.Column(db.Integer, nullable=False, server_default='0')
    count_uploaded_files = db.Column(db.Integer, nullable=False, server_default='0')
    count_uploaded_files_mo = db.Column(db.Integer, nullable=False, server_default='0')
    count_file_downloads = db.Column(db.Integer, nullable=False, server_default='0')
    is_admin = db.Column(db.Boolean, nullable=False, server_default='0')
    password_hash = db.Column(db.String(256), nullable=True, server_default=None)
    use_password = db.Column(db.Boolean, nullable=False, server_default='0')

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

# Define the admin model
class Admin(db.Model):
    __tablename__ = 'admin'
    id = db.Column(db.Integer, primary_key=True)
    last_daily_cron = db.Column(db.DateTime, nullable=False, 
                               server_default=func.datetime('2024-01-01 00:00:00'))

    def __repr__(self):
        return f"<Admin {self.id}>"

# Define the settings model
class Setting(db.Model):
    __tablename__ = 'setting'
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    aws_access_key_id = db.Column(db.String(200), nullable=True, server_default='')
    aws_secret_access_key = db.Column(db.String(200), nullable=True, server_default='')
    bucket_name = db.Column(db.String(200), nullable=True, server_default='')
    max_file_size = db.Column(db.Integer, nullable=False, server_default='1073741824')
    use_cloud = db.Column(db.Boolean, nullable=False, server_default='0')
    device_name_includes = db.Column(db.String(100), nullable=True, server_default='HUBLINK')
    alert_email = db.Column(db.String(100), nullable=True, server_default='')
    gateway_manages_memory = db.Column(db.Boolean, nullable=False, server_default='1')
    timezone = db.Column(db.String(50), nullable=False, server_default='America/Chicago')

    def __repr__(self):
        return f"<Setting for Account {self.account_id}>"

    def to_dict(self):
        return {
            'aws_access_key_id': self.aws_access_key_id,
            'aws_secret_access_key': self.aws_secret_access_key,
            'bucket_name': self.bucket_name,
            'max_file_size': self.max_file_size,
            'use_cloud': self.use_cloud,
            'device_name_includes': self.device_name_includes,
            'alert_email': self.alert_email,
            'gateway_manages_memory': self.gateway_manages_memory,
            'timezone': self.timezone
        }

# Define the files model
class File(db.Model):
    __tablename__ = 'file'
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    key = db.Column(db.String(200), nullable=False, server_default='')
    url = db.Column(db.String(500), nullable=False, server_default='')
    size = db.Column(db.Integer, nullable=False, server_default='0')
    last_modified = db.Column(db.DateTime, nullable=False, server_default=func.now())
    version = db.Column(db.Integer, nullable=False, server_default='1')
    last_checked = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f"<File {self.key} for Account {self.account_id}>"

    def to_dict(self):
        """Convert file object to dictionary."""
        utc_last_modified = self.last_modified.replace(tzinfo=timezone.utc) if self.last_modified else None
        utc_last_checked = self.last_checked.replace(tzinfo=timezone.utc) if self.last_checked else None
        
        return {
            'key': self.key,
            'url': self.url,
            'size': self.size,
            'last_modified': utc_last_modified.isoformat() if utc_last_modified else None,
            'last_checked': utc_last_checked.isoformat() if utc_last_checked else None,
            'version': self.version
        }

# Define the device model with ip_address instead of mac_address
class Gateway(db.Model):
    __tablename__ = 'gateway'
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    ip_address = db.Column(db.String(45), nullable=False, server_default='')  # IPv4 is 15 characters max, IPv6 is up to 45 characters
    name = db.Column(db.String(100), nullable=True, server_default='')
    created_at = db.Column(db.DateTime, nullable=False, server_default=func.now())

    # Add relationship with nodes
    nodes = db.relationship('Node', backref='gateway', cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Gateway {self.name} with IP {self.ip_address}>"

    def to_dict(self):
        return {
            'id': self.id,
            'account_id': self.account_id,
            'ip_address': self.ip_address,
            'name': self.name,
            'created_at': self.created_at.replace(tzinfo=timezone.utc).isoformat() if self.created_at else None
        }

# Define the node model
class Node(db.Model):
    __tablename__ = 'node'
    id = db.Column(db.Integer, primary_key=True)
    gateway_id = db.Column(db.Integer, db.ForeignKey('gateway.id', ondelete='CASCADE'), nullable=False)
    uuid = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, server_default=func.now(), nullable=False)

    def __repr__(self):
        return f'<Node {self.uuid}>'

    def to_dict(self):
        return {
            'id': self.id,
            'gateway_id': self.gateway_id,
            'uuid': self.uuid,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

# Define the source model
class Source(db.Model):
    __tablename__ = 'source'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, server_default='')
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    directory_filter = db.Column(db.String(200), nullable=False, server_default='*')
    include_subdirs = db.Column(db.Boolean, nullable=False, server_default='0')
    include_columns = db.Column(db.String(500), nullable=False, server_default='')
    data_points = db.Column(db.Integer, nullable=False, server_default='0')
    tail_only = db.Column(db.Boolean, nullable=False, server_default='0')
    datetime_column = db.Column(db.String(100), nullable=False, server_default='')  # New field for datetime column selection
    last_updated = db.Column(db.DateTime, nullable=True)
    error = db.Column(db.String(500), nullable=True, server_default='')
    file_id = db.Column(db.Integer, db.ForeignKey('file.id', name='fk_source_file'), nullable=True)
    state = db.Column(db.String(50), nullable=False, server_default='created')
    file = db.relationship('File', backref=db.backref('sources', lazy=True))
    max_path_level = db.Column(db.Integer, nullable=False, server_default='0')  # Store the maximum path level for grouping
    do_update = db.Column(db.Boolean, nullable=False, server_default='0')  # New field for deferred updates

    def __repr__(self):
        return f"<Source {self.name} for Account {self.account_id}>"

    def to_dict(self):
        """Convert source to dictionary with dynamic state"""
        data = {
            'id': self.id,
            'name': self.name,
            'directory_filter': self.directory_filter,
            'include_columns': self.include_columns,
            'include_subdirs': self.include_subdirs,
            'data_points': self.data_points,
            'tail_only': self.tail_only,
            'datetime_column': self.datetime_column,
            'last_updated': self.last_updated.replace(tzinfo=timezone.utc).isoformat() if self.last_updated else None,
            'state': self.state,
            'error': self.error,
            'file_id': self.file_id,
            'file_size': self.file.size if self.file else 0,
            'max_path_level': self.max_path_level
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
    __tablename__ = 'plot'
    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey('source.id', ondelete='CASCADE'), nullable=False)
    name = db.Column(db.String(100), nullable=False, server_default='')
    type = db.Column(db.String(50), nullable=False, server_default='timeline')
    config = db.Column(db.String(500), nullable=False, server_default='{}')  # JSON string
    group_by = db.Column(db.Integer, nullable=True)  # Add group_by field
    advanced = db.Column(db.String(500), nullable=False, server_default='[]')  # JSON string for advanced options
    
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
            'group_by': self.group_by,  # Add group_by to dict output
            'advanced': json.loads(self.advanced)  # Add advanced options to dict output
        }

# Define the layout model
class Layout(db.Model):
    __tablename__ = 'layout'
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False, server_default='')
    config = db.Column(db.Text, nullable=False, server_default='{}')  # JSON string storing grid layout
    created_at = db.Column(db.DateTime, server_default=func.now())
    is_default = db.Column(db.Boolean, nullable=False, server_default='0')
    show_nav = db.Column(db.Boolean, nullable=False, server_default='0')
    time_range = db.Column(db.String(20), nullable=False, server_default='all')
    
    def __repr__(self):
        return f"<Layout {self.name} for Account {self.account_id}>"

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'config': json.loads(self.config),
            'created_at': self.created_at.replace(tzinfo=timezone.utc).isoformat() if self.created_at else None,
            'is_default': self.is_default,
            'show_nav': self.show_nav
        }
