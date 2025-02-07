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
        tz = pytz.timezone(tz_name)
        
        # Get current time in UTC first, then convert to target timezone
        now = datetime.now(timezone.utc)
        now = now.astimezone(tz)
        
        # If datetime is naive (from SQLite), assume it's UTC
        if dt.tzinfo is None:
            utc_dt = dt.replace(tzinfo=timezone.utc)  # First make it UTC
            local_dt = utc_dt.astimezone(tz)  # Then convert to local
        else:
            # If datetime has timezone info, convert it
            local_dt = dt.astimezone(tz)
            
    except pytz.exceptions.UnknownTimeZoneError:
        # Fallback to UTC if timezone is invalid
        tz = timezone.utc
        now = datetime.now(timezone.utc)
        local_dt = dt if dt.tzinfo else dt.replace(tzinfo=tz)
        logging.warning(f"Unknown timezone: {tz_name}, falling back to UTC")
    
    if format == 'relative':
        # Calculate time difference - ensure both are timezone-aware
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
            return redirect(url_for('admin'))
        
        account = db.session.get(Account, session['admin_id'])
        if not account or not account.is_admin:
            session.pop('admin_id', None)
            return redirect(url_for('admin'))
            
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
        
        # Base query for gateways
        gateways_query = Gateway.query
        if account_id:
            gateways_query = gateways_query.filter_by(account_id=account_id)
        
        # Get unique gateway count by name only
        total_gateways = db.session.query(
            func.count(distinct(Gateway.name))
        ).filter(
            Gateway.id.in_(gateways_query.with_entities(Gateway.id))
        ).scalar() or 0
        
        # Get 24h metrics
        files_24h = File.query
        gateways_24h = Gateway.query
        nodes_24h = Node.query
        
        if account_id:
            files_24h = files_24h.filter_by(account_id=account_id)
            gateways_24h = gateways_24h.filter_by(account_id=account_id)
            nodes_24h = nodes_24h.join(Gateway).filter(Gateway.account_id == account_id)
        
        # Count unique gateways in last 24h by name
        gateways_24h_count = db.session.query(
            func.count(distinct(Gateway.name))
        ).filter(
            Gateway.created_at >= last_24h,
            Gateway.id.in_(gateways_24h.with_entities(Gateway.id))
        ).scalar() or 0
        
        # Count unique nodes in last 24h by UUID
        nodes_24h_count = db.session.query(
            func.count(distinct(Node.uuid))
        ).filter(
            Node.created_at >= last_24h,
            Node.id.in_(nodes_24h.with_entities(Node.id))
        ).scalar() or 0
        
        # Count files in last 24h
        files_24h_count = files_24h.filter(File.last_modified >= last_24h).count()
        
        # Get total nodes count (unique UUIDs)
        total_nodes = db.session.query(
            func.count(distinct(Node.uuid))
        ).filter(
            Node.id.in_(nodes_24h.with_entities(Node.id))
        ).scalar() or 0
        
        analytics = {
            'total_accounts': len(accounts),
            'total_gateways': total_gateways,
            'total_gateway_pings': sum(account.count_gateway_pings for account in accounts),
            'total_page_loads': sum(account.count_page_loads for account in accounts),
            'active_accounts': len([acc for acc in accounts if acc.count_page_loads > 0]),
            'total_file_downloads': sum(account.count_file_downloads for account in accounts),
            'total_settings_updated': sum(account.count_settings_updated for account in accounts),
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