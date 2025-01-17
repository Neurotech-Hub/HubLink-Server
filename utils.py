from functools import wraps
from flask import session, redirect, url_for
from models import db, Account, Gateway
from sqlalchemy import distinct, func

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
        
        # Get unique gateway count using a subquery to count distinct combinations
        total_gateways = db.session.query(
            Gateway.name, Gateway.ip_address
        ).filter(
            Gateway.id.in_(gateways_query.with_entities(Gateway.id))
        ).group_by(
            Gateway.name, Gateway.ip_address
        ).count()
        
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