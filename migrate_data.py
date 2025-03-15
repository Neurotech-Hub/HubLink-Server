import os
import json
from datetime import datetime, timezone
import sqlite3
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv
import logging
import subprocess

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('migration.log'),
        logging.StreamHandler()
    ]
)

# Load environment variables
load_dotenv()

# Configuration
SQLITE_DB_PATH = 'instance/accounts.db'
POSTGRES_URL = os.getenv('DATABASE_URL', 'postgresql://localhost/hublink_dev')

# Table migration order (respecting foreign key constraints)
TABLES_TO_MIGRATE = [
    'account',      # No foreign keys
    'admin',       # No foreign keys
    'setting',     # Depends on account
    'file',        # Depends on account
    'gateway',     # Depends on account
    'source',      # Depends on account, file
    'node',        # Depends on gateway
    'plot',        # Depends on source
    'layout'       # Depends on account
]

def backup_sqlite_db():
    """Create a backup of the SQLite database."""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_dir = 'backups'
    if not os.path.exists(backup_dir):
        os.makedirs(backup_dir)
    
    backup_path = f"{backup_dir}/accounts_{timestamp}.db"
    try:
        import shutil
        shutil.copy2(SQLITE_DB_PATH, backup_path)
        logging.info(f"Created backup at {backup_path}")
        return True
    except Exception as e:
        logging.error(f"Failed to create backup: {e}")
        return False

def get_table_columns(sqlite_cur, table_name):
    """Get column names and types for a table."""
    sqlite_cur.execute(f"PRAGMA table_info({table_name})")
    return {row[1]: row[2] for row in sqlite_cur.fetchall()}

def convert_sqlite_value(value, column_type):
    """Convert SQLite value to PostgreSQL compatible value."""
    if value is None:
        return None
    
    # Handle boolean values
    if column_type.lower() == 'boolean':
        if isinstance(value, str):
            return value.lower() == 'true'
        return bool(int(value))
    
    # Handle JSON fields
    if column_type.lower() in ['jsonb', 'json']:
        if isinstance(value, str):
            try:
                # Ensure we have a valid JSON string
                parsed_value = json.loads(value)
                # Convert back to string for PostgreSQL JSONB
                return json.dumps(parsed_value)
            except json.JSONDecodeError:
                # If it's not valid JSON, store as a JSON string
                return json.dumps(value)
        else:
            # If it's already a Python object, convert to JSON string
            return json.dumps(value)
    
    # Handle timestamp fields
    if 'datetime' in column_type.lower():
        if isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
                return dt.astimezone(timezone.utc)
            except ValueError:
                return value
        return value
    
    return value

def verify_foreign_keys(pg_cur, table_name, rows):
    """Verify that foreign key references exist before inserting."""
    try:
        # Get foreign key constraints for the table
        pg_cur.execute("""
            SELECT
                kcu.column_name,
                ccu.table_name AS foreign_table_name,
                ccu.column_name AS foreign_column_name,
                c.data_type
            FROM information_schema.table_constraints AS tc
            JOIN information_schema.key_column_usage AS kcu
                ON tc.constraint_name = kcu.constraint_name
            JOIN information_schema.constraint_column_usage AS ccu
                ON ccu.constraint_name = tc.constraint_name
            JOIN information_schema.columns c
                ON c.table_name = ccu.table_name
                AND c.column_name = ccu.column_name
            WHERE tc.constraint_type = 'FOREIGN KEY'
                AND tc.table_name = %s;
        """, (table_name,))
        
        foreign_keys = pg_cur.fetchall()
        
        if not foreign_keys:
            return True  # No foreign keys to verify
            
        # Get column names for the table
        pg_cur.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = %s
            ORDER BY ordinal_position;
        """, (table_name,))
        columns = [col[0] for col in pg_cur.fetchall()]
        
        # Check each foreign key constraint
        for fk_column, fk_table, fk_ref_column, data_type in foreign_keys:
            col_idx = columns.index(fk_column)
            fk_values = set(row[col_idx] for row in rows if row[col_idx] is not None)
            
            if fk_values:
                # Cast values based on data type
                if data_type == 'integer':
                    fk_values = set(int(val) for val in fk_values)
                elif data_type == 'boolean':
                    fk_values = set(bool(val) for val in fk_values)
                
                # Check if referenced values exist
                placeholders = ','.join(['%s'] * len(fk_values))
                pg_cur.execute(f"""
                    SELECT {fk_ref_column}
                    FROM {fk_table}
                    WHERE {fk_ref_column} IN ({placeholders})
                """, tuple(fk_values))
                
                existing_values = set(row[0] for row in pg_cur.fetchall())
                missing_values = fk_values - existing_values
                
                if missing_values:
                    logging.error(f"Missing foreign key values in {fk_table}.{fk_ref_column}: {missing_values}")
                    return False
                    
        return True
        
    except Exception as e:
        logging.error(f"Error verifying foreign keys for {table_name}: {e}")
        return False

def migrate_table_data(sqlite_cur, pg_cur, table_name):
    """Migrate data from SQLite to PostgreSQL for a single table."""
    try:
        # Get column information
        columns = get_table_columns(sqlite_cur, table_name)
        column_names = list(columns.keys())
        
        # Get data from SQLite
        sqlite_cur.execute(f"SELECT * FROM {table_name}")
        rows = sqlite_cur.fetchall()
        
        if not rows:
            logging.info(f"No data to migrate for table {table_name}")
            return 0
        
        # Convert data for PostgreSQL
        converted_rows = []
        for row in rows:
            converted_row = [
                convert_sqlite_value(val, columns[col])
                for val, col in zip(row, column_names)
            ]
            converted_rows.append(converted_row)
        
        # Verify foreign key constraints
        if not verify_foreign_keys(pg_cur, table_name, converted_rows):
            raise Exception(f"Foreign key verification failed for table {table_name}")
        
        # Prepare column names string
        columns_str = ', '.join(column_names)
        
        # Disable triggers temporarily to allow setting IDs
        pg_cur.execute(f"ALTER TABLE {table_name} DISABLE TRIGGER ALL")
        
        try:
            # Use execute_values for efficient batch insert
            execute_values(
                pg_cur,
                f"INSERT INTO {table_name} ({columns_str}) VALUES %s",
                converted_rows,
                template=None,
                page_size=100
            )
            
            # Update sequence if this table has an ID column
            if 'id' in column_names:
                max_id = max(row[column_names.index('id')] for row in converted_rows)
                pg_cur.execute(f"SELECT setval('{table_name}_id_seq', {max_id}, true)")
                
        finally:
            # Re-enable triggers
            pg_cur.execute(f"ALTER TABLE {table_name} ENABLE TRIGGER ALL")
        
        return len(rows)
    except Exception as e:
        logging.error(f"Error migrating table {table_name}: {e}")
        raise

def clean_orphaned_records(sqlite_cur):
    """Remove records that reference non-existent accounts and cascade through relationships."""
    try:
        # Get all account IDs
        sqlite_cur.execute("SELECT id FROM account")
        valid_account_ids = {row[0] for row in sqlite_cur.fetchall()}
        
        # Clean up in reverse dependency order
        cleanup_order = [
            # Level 3 dependencies
            ('plot', 'source_id', 'source'),
            ('node', 'gateway_id', 'gateway'),
            # Level 2 dependencies
            ('source', 'file_id', 'file'),
            ('source', 'account_id', 'account'),
            # Level 1 dependencies
            ('file', 'account_id', 'account'),
            ('layout', 'account_id', 'account'),
            ('setting', 'account_id', 'account'),
            ('gateway', 'account_id', 'account'),
        ]
        
        total_cleaned = 0
        for table, fk_column, parent_table in cleanup_order:
            # Get parent IDs
            sqlite_cur.execute(f"SELECT id FROM {parent_table}")
            valid_parent_ids = {row[0] for row in sqlite_cur.fetchall()}
            
            # Count and clean orphaned records
            sqlite_cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {fk_column} NOT IN (SELECT id FROM {parent_table})")
            orphaned_count = sqlite_cur.fetchone()[0]
            
            if orphaned_count > 0:
                sqlite_cur.execute(f"DELETE FROM {table} WHERE {fk_column} NOT IN (SELECT id FROM {parent_table})")
                total_cleaned += orphaned_count
                logging.info(f"Cleaned {orphaned_count} orphaned records from {table} (referencing {parent_table})")
        
        return total_cleaned
    except Exception as e:
        logging.error(f"Error cleaning orphaned records: {e}")
        raise

def run_flask_migrations():
    """Run Flask database migrations to create schema."""
    try:
        logging.info("Running Flask migrations to create schema...")
        result = subprocess.run(['flask', 'db', 'upgrade'], 
                              capture_output=True, 
                              text=True)
        if result.returncode == 0:
            logging.info("Flask migrations completed successfully")
            return True
        else:
            logging.error(f"Flask migrations failed: {result.stderr}")
            return False
    except Exception as e:
        logging.error(f"Error running Flask migrations: {e}")
        return False

def migrate_data():
    """Main migration function."""
    if not backup_sqlite_db():
        logging.error("Failed to create backup. Aborting migration.")
        return False
        
    # Run Flask migrations first to create schema
    if not run_flask_migrations():
        logging.error("Failed to create database schema. Aborting migration.")
        return False
    
    sqlite_conn = None
    pg_conn = None
    
    try:
        # Connect to SQLite
        sqlite_conn = sqlite3.connect(SQLITE_DB_PATH)
        sqlite_cur = sqlite_conn.cursor()
        
        # Clean orphaned records first
        logging.info("Cleaning orphaned records...")
        total_cleaned = clean_orphaned_records(sqlite_cur)
        logging.info(f"Cleaned {total_cleaned} total orphaned records")
        
        # Commit the cleanup
        sqlite_conn.commit()
        
        # Connect to PostgreSQL with autocommit to handle transaction management manually
        pg_conn = psycopg2.connect(POSTGRES_URL)
        pg_conn.autocommit = True  # Set autocommit mode initially
        pg_cur = pg_conn.cursor()
        
        # Start migration
        total_records = 0
        
        # Disable foreign key constraints at session level
        pg_cur.execute("SET session_replication_role = 'replica';")
        
        # Now disable autocommit for the actual data migration
        pg_conn.autocommit = False
        
        try:
            for table in TABLES_TO_MIGRATE:
                logging.info(f"Migrating table: {table}")
                try:
                    # Get column information
                    columns = get_table_columns(sqlite_cur, table)
                    column_names = list(columns.keys())
                    
                    # Get data from SQLite
                    sqlite_cur.execute(f"SELECT * FROM {table}")
                    rows = sqlite_cur.fetchall()
                    
                    if not rows:
                        logging.info(f"No data to migrate for table {table}")
                        continue
                    
                    # Convert data for PostgreSQL
                    converted_rows = []
                    for row in rows:
                        converted_row = [
                            convert_sqlite_value(val, columns[col])
                            for val, col in zip(row, column_names)
                        ]
                        converted_rows.append(converted_row)
                    
                    # Prepare column names string
                    columns_str = ', '.join(column_names)
                    
                    # Use execute_values for efficient batch insert
                    execute_values(
                        pg_cur,
                        f"INSERT INTO {table} ({columns_str}) VALUES %s",
                        converted_rows,
                        template=None,
                        page_size=100
                    )
                    
                    # Update sequence if this table has an ID column
                    if 'id' in column_names:
                        max_id = max(row[column_names.index('id')] for row in converted_rows)
                        pg_cur.execute(f"SELECT setval('{table}_id_seq', {max_id}, true)")
                    
                    records_migrated = len(rows)
                    total_records += records_migrated
                    logging.info(f"Migrated {records_migrated} records from {table}")
                    
                    # Commit after each table
                    pg_conn.commit()
                    
                except Exception as table_error:
                    logging.error(f"Error migrating table {table}: {table_error}")
                    pg_conn.rollback()
                    raise
            
            logging.info(f"Migration completed successfully. Total records migrated: {total_records}")
            return True
            
        finally:
            # Re-enable foreign key constraints
            pg_conn.autocommit = True
            pg_cur.execute("SET session_replication_role = 'origin';")
        
    except Exception as e:
        logging.error(f"Migration failed: {str(e)}")
        if pg_conn and not pg_conn.closed:
            pg_conn.rollback()
        return False
        
    finally:
        # Close connections
        if sqlite_conn:
            sqlite_conn.close()
        if pg_conn:
            pg_conn.close()

if __name__ == '__main__':
    try:
        logging.info("Starting migration process...")
        success = migrate_data()
        if success:
            logging.info("Migration completed successfully")
        else:
            logging.error("Migration failed")
    except KeyboardInterrupt:
        logging.info("Migration interrupted by user")
    except Exception as e:
        logging.error(f"Unexpected error during migration: {e}") 