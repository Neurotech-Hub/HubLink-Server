from functools import wraps
from flask import session, redirect, url_for
from models import db, Account, Gateway, File
from sqlalchemy import distinct, func
import logging
import requests
import os
import re

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
        
        analytics = {
            'total_accounts': len(accounts),
            'total_gateways': total_gateways,
            'total_gateway_pings': sum(account.count_gateway_pings for account in accounts),
            'total_page_loads': sum(account.count_page_loads for account in accounts),
            'active_accounts': len([acc for acc in accounts if acc.count_page_loads > 0]),
            'total_file_downloads': sum(account.count_file_downloads for account in accounts),
            'total_settings_updated': sum(account.count_settings_updated for account in accounts),
            'total_uploaded_files': sum(account.count_uploaded_files for account in accounts)
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