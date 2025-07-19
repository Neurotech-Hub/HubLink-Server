from functools import wraps
from flask import session, redirect, url_for
from models import db, Account, Gateway, File, Node
from sqlalchemy import distinct, func
import logging
import requests
import os
import re
from datetime import datetime, timezone, timedelta
import pytz

def format_datetime(dt, tz_name='America/Chicago', format='relative'):
    """
    Format a datetime object according to the specified timezone and format.
    
    Args:
        dt: datetime object (if naive, assumed to be UTC)
        tz_name: timezone name (from account settings)
        format: 'relative' for "X time ago" or 'absolute' for "YYYY-MM-DD HH:MM"
    
    Returns:
        Formatted string representation of the datetime
    """
    if not dt:
        return "Never"
    
    try:
        # Get the target timezone
        tz = pytz.timezone(tz_name)
        
        # Ensure dt is timezone-aware UTC first
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
        elif dt.tzinfo != pytz.utc:
            dt = dt.astimezone(pytz.utc)
            
        # Convert to target timezone
        local_dt = dt.astimezone(tz)
        
        # Get current time in target timezone
        now = datetime.now(tz)
        
    except pytz.exceptions.UnknownTimeZoneError:
        # Fallback to UTC if timezone is invalid
        logging.warning(f"Unknown timezone: {tz_name}, falling back to UTC")
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
        local_dt = dt
        now = datetime.now(pytz.utc)
    
    if format == 'relative':
        # Calculate time difference
        diff = now - local_dt
        
        if diff.total_seconds() < 60:
            return 'just now'
        elif diff.total_seconds() < 3600:
            minutes = int(diff.total_seconds() / 60)
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        elif diff.total_seconds() < 86400:
            hours = int(diff.total_seconds() / 3600)
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        elif diff.days < 7:
            return f"{diff.days} day{'s' if diff.days != 1 else ''} ago"
        else:
            return local_dt.strftime("%Y-%m-%d %H:%M")
    else:
        return local_dt.strftime("%Y-%m-%d %H:%M")

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_id' not in session:
            return redirect(url_for('admin_login'))
        
        account = db.session.get(Account, session['admin_id'])
        if not account or not account.is_admin:
            session.pop('admin_id', None)
            return redirect(url_for('admin_login'))
            
        return f(*args, **kwargs)
    return decorated_function

def get_analytics(account_id=None):
    """
    Get analytics for either a specific account or all accounts.
    
    Args:
        account_id: Optional ID of specific account. If None, gets analytics for all accounts.
    
    Returns:
        Dictionary containing analytics data.
    """
    try:
        # Get current time in UTC for 24h calculations
        now = datetime.now(timezone.utc)
        last_24h = now - timedelta(hours=24)
        
        # Base query for accounts
        accounts_query = Account.query
        if account_id:
            accounts_query = accounts_query.filter_by(id=account_id)
        
        # Get all relevant accounts
        accounts = accounts_query.all()
        
        # Optimized queries using indexes
        if account_id:
            # Single account queries - much faster with indexes
            total_gateways = db.session.query(
                func.count(distinct(Gateway.name))
            ).filter_by(account_id=account_id).scalar() or 0
            
            # 24h metrics for single account
            gateways_24h_count = db.session.query(
                func.count(distinct(Gateway.name))
            ).filter(
                Gateway.account_id == account_id,
                Gateway.created_at >= last_24h
            ).scalar() or 0
            
            nodes_24h_count = db.session.query(
                func.count(distinct(Node.uuid))
            ).join(Gateway).filter(
                Gateway.account_id == account_id,
                Node.created_at >= last_24h
            ).scalar() or 0
            
            files_24h_count = db.session.query(
                func.count(File.id)
            ).filter(
                File.account_id == account_id,
                File.last_modified >= last_24h
            ).scalar() or 0
            
            total_nodes = db.session.query(
                func.count(distinct(Node.uuid))
            ).join(Gateway).filter(
                Gateway.account_id == account_id
            ).scalar() or 0
            
        else:
            # All accounts queries
            total_gateways = db.session.query(
                func.count(distinct(Gateway.name))
            ).scalar() or 0
            
            # 24h metrics for all accounts
            gateways_24h_count = db.session.query(
                func.count(distinct(Gateway.name))
            ).filter(Gateway.created_at >= last_24h).scalar() or 0
            
            nodes_24h_count = db.session.query(
                func.count(distinct(Node.uuid))
            ).filter(Node.created_at >= last_24h).scalar() or 0
            
            files_24h_count = db.session.query(
                func.count(File.id)
            ).filter(File.last_modified >= last_24h).scalar() or 0
            
            total_nodes = db.session.query(
                func.count(distinct(Node.uuid))
            ).scalar() or 0
        
        analytics = {
            'total_accounts': len(accounts),
            'total_gateways': total_gateways,
            'total_gateway_pings': sum(account.count_gateway_pings for account in accounts),
            'total_file_downloads': sum(account.count_file_downloads for account in accounts),
            'total_uploaded_files': sum(account.count_uploaded_files for account in accounts),
            'total_nodes': total_nodes,
            # Add 24h metrics
            'files_24h': files_24h_count,
            'gateways_24h': gateways_24h_count,
            'nodes_24h': nodes_24h_count
        }
        
        return analytics
        
    except Exception as e:
        print(f"Error getting analytics: {e}")
        return None

def get_database_statistics():
    """
    Get global database statistics showing total record counts for all tables.
    This is for admin dashboard use only.
    
    Returns:
        Dictionary containing database table statistics.
    """
    try:
        from models import Setting, Source, Plot, Layout
        
        # Get total counts for all tables (no filtering by account)
        statistics = {
            'total_accounts': Account.query.count(),
            'total_files': File.query.count(),
            'total_gateways': Gateway.query.count(),
            'total_nodes': Node.query.count(),
            'total_sources': Source.query.count(),
            'total_plots': Plot.query.count(),
            'total_layouts': Layout.query.count(),
            'total_settings': Setting.query.count()
        }
        
        return statistics
        
    except Exception as e:
        print(f"Error getting database statistics: {e}")
        return None
    
def list_source_files(account, source):
    """Returns a list of files that match the source's directory filter pattern.
    Takes a source and a list of files and returns which files match the source pattern.
    Does NOT rebuild or fetch files from S3.
    """
    try:
        # Get existing files from database
        files = File.query.filter_by(account_id=account.id)\
            .filter(~File.key.like('.%'))\
            .filter(~File.key.contains('/.')) \
            .all()
        
        # Convert glob pattern to regex
        clean_dir = source.directory_filter.strip('/')
        if source.include_subdirs:
            file_filter = f"{clean_dir}/**/*.[cC][sS][vV]"
        else:
            file_filter = f"{clean_dir}/*.[cC][sS][vV]"
            
        # Convert glob pattern to regex
        if '**' in file_filter:
            # Handle recursive matching
            parts = file_filter.split('**')
            prefix = parts[0].lstrip('/')
            # For directory matching, we want exact prefix match
            pattern_str = f"^{prefix}.*[^/]+\\.csv$"
            pattern = re.compile(pattern_str, re.IGNORECASE)
        elif '*' in file_filter:
            # Handle single-level matching
            file_filter = file_filter.lstrip('/')
            base_pattern = file_filter.replace('*.[cC][sS][vV]', '')
            pattern_str = f"^{base_pattern}[^/]+\\.csv$"
            pattern = re.compile(pattern_str, re.IGNORECASE)
        else:
            pattern = re.compile(file_filter)
            
        # Find matching files
        matching_files = []
        for file in files:
            if pattern.match(file.key):
                matching_files.append(file)
                
        return matching_files
    except Exception as e:
        logging.error(f"Error in list_source_files for source {source.id}: {e}")
        return []

def initiate_source_refresh(account, source):
    """
    Initiates a refresh for a source without any HTTP redirects.
    Returns (success, error_message) tuple.
    """
    try:
        # Reset source status
        source.success = False
        source.error = None
        source.file_id = None
        source.state = 'running'
        source.do_update = False  # Set do_update to False when refresh is initiated
        # Prepare payload for lambda
        payload = {
            'source': {
                'name': source.name,
                'id': source.id,
                'directory_filter': source.directory_filter,
                'include_subdirs': source.include_subdirs,
                'include_columns': source.include_columns,
                'data_points': source.data_points,
                'tail_only': source.tail_only,
                'account_url': account.url
            }
        }
        
        lambda_url = os.environ.get('LAMBDA_URL')
        if not lambda_url:
            raise ValueError("LAMBDA_URL environment variable not set")
            
        try:
            requests.post(lambda_url, json=payload, timeout=0.1)  # timeout right away
        except requests.exceptions.Timeout:
            # This is expected, ignore it
            pass
        
        db.session.commit()
        return True, None
        
    except Exception as e:
        error_msg = str(e)
        logging.error(f"Error refreshing source {source.id}: {error_msg}")
        if not source.error:  # Only set error if not already set
            source.error = error_msg
            source.state = 'error'
            db.session.commit()
        return False, error_msg 

def format_file_size(size_in_bytes):
    """Format file size to human readable format (B, KB, MB, GB).
    
    Args:
        size_in_bytes: Integer representing file size in bytes
        
    Returns:
        String representation of the file size with appropriate unit
    """
    if size_in_bytes is None:
        return '0 B'
        
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_in_bytes < 1024:
            if unit == 'B':
                return f"{int(size_in_bytes)} {unit}"
            return f"{size_in_bytes:.1f} {unit}"
        size_in_bytes /= 1024
    return f"{size_in_bytes:.1f} GB" 