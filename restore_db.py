#!/usr/bin/env python3

"""
PostgreSQL Database Restore Tool

This tool restores database backups to local development or staging environments.
It supports both file-based backups (.dir.tar.gz) and direct database connections.

Configuration File (.restore):
The tool uses a .restore file to store database connection URLs securely.
This file is automatically added to .gitignore to prevent credentials from being tracked.

Format of .restore file:
[databases]
production = postgresql://user:pass@prod-host/db_prod
staging = postgresql://user:pass@stag-host/db_stag
local = hublink_dev

- production: Optional. Used for direct database pulls (must contain 'prod' for safety)
- staging: Required. Target for staging restores (must contain 'stag' for safety)
- local: Optional. Local database name (defaults to 'hublink_dev')

SAFETY REQUIREMENTS:
- Production URLs MUST contain 'prod' to prevent staging overwrites
- Staging URLs MUST contain 'stag' to prevent production overwrites
- These validations prevent accidental data loss from misconfigured URLs

Usage Examples:
1. First time setup: python restore_db.py (will prompt for configurations)
2. Direct production to local: Choose source=2, target=1
3. Direct production to staging: Choose source=2, target=2
4. File-based restore: Choose source=1, provide backup file path

Safety Features:
- Production URLs must contain 'prod' to prevent staging overwrites
- Staging URLs must contain 'stag' to prevent production overwrites
- All connections are tested before proceeding
- Temporary files are automatically cleaned up
- Configuration file is excluded from git tracking
"""

import os
import sys
import subprocess
import tempfile
import shutil
from pathlib import Path
import tarfile
import re
from urllib.parse import urlparse
import json
import configparser

# Add PostgreSQL 16 tools to PATH
pg16_bin_path = "/opt/homebrew/opt/postgresql@16/bin"
if os.path.exists(pg16_bin_path):
    current_path = os.environ.get('PATH', '')
    os.environ['PATH'] = f"{pg16_bin_path}:{current_path}"

# Configuration file
RESTORE_CONFIG_FILE = '.restore'

def load_restore_config():
    """Load database configurations from .restore file."""
    config = {
        'production': None,
        'staging': None,
        'local': 'hublink_dev'
    }
    
    if os.path.exists(RESTORE_CONFIG_FILE):
        parser = configparser.ConfigParser()
        parser.read(RESTORE_CONFIG_FILE)
        
        if 'databases' in parser:
            config['production'] = parser.get('databases', 'production', fallback=None)
            config['staging'] = parser.get('databases', 'staging', fallback=None)
            config['local'] = parser.get('databases', 'local', fallback='hublink_dev')
    
    return config

def save_restore_config(config):
    """Save database configurations to .restore file."""
    parser = configparser.ConfigParser()
    parser.add_section('databases')
    
    if config['production']:
        parser.set('databases', 'production', config['production'])
    if config['staging']:
        parser.set('databases', 'staging', config['staging'])
    parser.set('databases', 'local', config['local'])
    
    with open(RESTORE_CONFIG_FILE, 'w') as f:
        parser.write(f)
    
    print(f"Configuration saved to {RESTORE_CONFIG_FILE}")

def setup_database_config():
    """Interactive setup for database configurations."""
    config = load_restore_config()
    
    print("\n=== Database Configuration Setup ===")
    print("This will create a .restore file with your database URLs.")
    print("The file will be added to .gitignore to keep credentials secure.\n")
    
    # Production database
    print("Production Database (optional - for direct production pulls):")
    prod_url = input("Production database URL (leave empty to skip): ").strip()
    if prod_url:
        is_valid, message = validate_production_url(prod_url)
        if not is_valid:
            print(f"Error: {message}")
            return None
        config['production'] = prod_url
    
    # Staging database
    print("\nStaging Database:")
    staging_url = input("Staging database URL: ").strip()
    if not staging_url:
        print("Error: Staging database URL is required")
        return None
    is_valid, message = validate_staging_url(staging_url)
    if not is_valid:
        print(f"Error: {message}")
        return None
    config['staging'] = staging_url
    
    # Local database name
    local_name = input(f"\nLocal database name (press Enter for '{config['local']}'): ").strip()
    if local_name:
        config['local'] = local_name
    
    # Save configuration
    save_restore_config(config)
    
    # Add to .gitignore if not already there
    gitignore_path = '.gitignore'
    if os.path.exists(gitignore_path):
        with open(gitignore_path, 'r') as f:
            content = f.read()
        
        if '.restore' not in content:
            with open(gitignore_path, 'a') as f:
                f.write('\n# Database restore configuration\n.restore\n')
            print("Added .restore to .gitignore")
    
    return config

def create_backup_from_database(source_url, backup_path):
    """Create a backup from a live database."""
    try:
        print(f"Creating backup from database...")
        
        # Create backup using pg_dump
        dump_cmd = [
            "pg_dump",
            "--format=custom",  # Custom format for pg_restore
            "--verbose",
            "--no-owner",
            "--no-privileges",
            "-f", backup_path,
            source_url
        ]
        
        result = subprocess.run(dump_cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            print(f"Error creating backup: {result.stderr}")
            return False
        
        print(f"✓ Backup created successfully: {backup_path}")
        return True
        
    except Exception as e:
        print(f"Error creating backup: {e}")
        return False

def check_postgres_tools():
    """Verify that required PostgreSQL tools are available."""
    required_tools = ['psql', 'createdb', 'dropdb', 'pg_restore', 'pg_dump']
    missing_tools = []
    
    for tool in required_tools:
        try:
            result = subprocess.run([tool, '--version'], capture_output=True, text=True)
            print(f"Using {tool}: {result.stdout.strip()}")
        except FileNotFoundError:
            missing_tools.append(tool)
    
    if missing_tools:
        print("Error: Required PostgreSQL tools not found:", ', '.join(missing_tools))
        print("Please ensure PostgreSQL command-line tools are installed and in your PATH")
        sys.exit(1)

def validate_production_url(db_url):
    """Validate that the database URL is for production environment."""
    if not db_url:
        return False, "Database URL is required"
    
    # Check for 'prod' in the URL (case insensitive)
    if 'prod' not in db_url.lower():
        return False, "Production database URL must contain 'prod' for safety (to avoid staging overwrites)"
    
    # Validate URL format
    try:
        parsed = urlparse(db_url)
        if parsed.scheme != 'postgresql':
            return False, "URL must use 'postgresql://' scheme"
        if not parsed.hostname:
            return False, "Invalid hostname in database URL"
        if not parsed.username or not parsed.password:
            return False, "Database URL must include username and password"
    except Exception as e:
        return False, f"Invalid database URL format: {e}"
    
    return True, "URL validation passed"

def validate_staging_url(db_url):
    """Validate that the database URL is for staging environment."""
    if not db_url:
        return False, "Database URL is required"
    
    # Check for 'stag' in the URL (case insensitive)
    if 'stag' not in db_url.lower():
        return False, "Staging database URL must contain 'stag' for safety (to avoid production overwrites)"
    
    # Validate URL format
    try:
        parsed = urlparse(db_url)
        if parsed.scheme != 'postgresql':
            return False, "URL must use 'postgresql://' scheme"
        if not parsed.hostname:
            return False, "Invalid hostname in database URL"
        if not parsed.username or not parsed.password:
            return False, "Database URL must include username and password"
    except Exception as e:
        return False, f"Invalid database URL format: {e}"
    
    return True, "URL validation passed"

def test_database_connection(db_url, name="database"):
    """Test connection to database."""
    try:
        print(f"Testing connection to {name}...")
        result = subprocess.run([
            "psql", db_url, "-c", "SELECT 1 as connection_test;"
        ], capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            print(f"✓ Connection to {name} successful")
            return True
        else:
            print(f"✗ Connection failed: {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        print(f"✗ Connection timeout - check your {name} URL and network connection")
        return False
    except Exception as e:
        print(f"✗ Connection error: {e}")
        return False

def extract_backup(backup_path, temp_dir):
    """Extract the backup file to a temporary directory."""
    try:
        print(f"Extracting backup file to temporary directory...")
        with tarfile.open(backup_path, 'r:gz') as tar:
            tar.extractall(path=temp_dir)
        
        # Find the actual backup directory (it's nested in date-named folders)
        backup_contents = list(Path(temp_dir).rglob('*.dat'))
        if not backup_contents:
            raise Exception("No database backup files found in archive")
        
        # Get the directory containing the .dat files
        backup_dir = backup_contents[0].parent
        return str(backup_dir)
    
    except Exception as e:
        print(f"Error extracting backup: {e}")
        sys.exit(1)

def restore_local_database(backup_path, db_name="hublink_dev"):
    """Restore the database to local development environment."""
    try:
        # Drop existing database if it exists
        print(f"Dropping existing database '{db_name}'...")
        subprocess.run(["dropdb", "-f", db_name], check=False)

        # Create fresh database
        print(f"Creating new database '{db_name}'...")
        subprocess.run(["createdb", db_name], check=True)

        # Restore from backup
        print("Restoring from backup...")
        restore_cmd = [
            "pg_restore",
            "--no-owner",     # Skip original ownership
            "--no-privileges", # Skip privilege assignments
            "-d", db_name,    # Target database
            "-j", "4",        # Use 4 parallel jobs
            backup_path
        ]
        
        result = subprocess.run(restore_cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            # pg_restore often returns non-zero even on successful restores
            # due to non-fatal errors, so we'll print warnings but not fail
            print("\nRestore completed with warnings:")
            print(result.stderr)
        else:
            print("\nRestore completed successfully!")
            
    except subprocess.CalledProcessError as e:
        print(f"Error during database restore: {e}")
        if e.stderr:
            print(f"Error output: {e.stderr}")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)

def restore_staging_database(backup_path, db_url):
    """Restore the database to staging environment."""
    try:
        print("Restoring to staging database...")
        
        # Restore from backup using the full database URL
        restore_cmd = [
            "pg_restore",
            "--no-owner",     # Skip original ownership
            "--no-privileges", # Skip privilege assignments
            "--clean",         # Clean (drop) database objects before recreating
            "--if-exists",     # Don't fail if objects don't exist
            "-d", db_url,     # Target database URL
            "-j", "4",        # Use 4 parallel jobs
            backup_path
        ]
        
        result = subprocess.run(restore_cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            # pg_restore often returns non-zero even on successful restores
            # due to non-fatal errors, so we'll print warnings but not fail
            print("\nRestore completed with warnings:")
            print(result.stderr)
        else:
            print("\nRestore completed successfully!")
            
    except subprocess.CalledProcessError as e:
        print(f"Error during staging database restore: {e}")
        if e.stderr:
            print(f"Error output: {e.stderr}")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)

def main():
    print("\n=== PostgreSQL Database Restore Tool ===")
    print("This tool will restore a database backup to your local database or staging environment.")
    
    # Load configuration
    config = load_restore_config()
    
    # If no config exists, offer to set it up
    if not config['staging']:
        print("No database configuration found.")
        setup_choice = input("Would you like to set up database configurations? (y/n): ").lower()
        if setup_choice == 'y':
            config = setup_database_config()
            if not config:
                print("Configuration setup cancelled.")
                return
        else:
            print("You can set up configurations later by running this script again.")
            print("For now, you can use file-based restores.")
    
    # Choose source type
    print("\nChoose source type:")
    print("1. Backup file (.dir.tar.gz)")
    print("2. Direct database connection")
    
    source_choice = input("\nEnter your choice (1 or 2): ").strip()
    
    backup_path = None
    temp_backup = None
    
    if source_choice == "1":
        # File-based backup
        while True:
            backup_path = input("\nEnter the path to your backup file (.dir.tar.gz): ").strip()
            if not backup_path:
                print("Please enter a valid path.")
                continue
                
            # Expand user directory if path starts with ~
            backup_path = os.path.expanduser(backup_path)
            
            if not os.path.exists(backup_path):
                print(f"Error: File not found: {backup_path}")
                continue
                
            if not backup_path.endswith('.dir.tar.gz'):
                print("Warning: File doesn't end with .dir.tar.gz. Are you sure this is a Render backup?")
                confirm = input("Continue anyway? (y/n): ").lower()
                if confirm != 'y':
                    continue
            
            break
            
    elif source_choice == "2":
        # Direct database connection
        if not config['production']:
            print("Error: No production database configured.")
            print("Please set up your database configurations first.")
            return
        
        print(f"\nUsing production database for backup...")
        is_valid, message = validate_production_url(config['production'])
        if not is_valid:
            print(f"Error: {message}")
            return
        if not test_database_connection(config['production'], "production"):
            print("Cannot proceed without a valid connection to production database.")
            return
        
        # Create temporary backup file
        temp_backup = tempfile.NamedTemporaryFile(suffix='.backup', delete=False)
        temp_backup.close()
        backup_path = temp_backup.name
        
        if not create_backup_from_database(config['production'], backup_path):
            print("Failed to create backup from production database.")
            return
        
        print(f"✓ Backup created: {backup_path}")
        
    else:
        print("Invalid choice. Please enter 1 or 2.")
        return
    
    # Choose target environment
    print("\nChoose target environment:")
    print("1. Local Development")
    print("2. Staging Environment")
    
    target_choice = input("\nEnter your choice (1 or 2): ").strip()
    
    if target_choice == "1":
        # Local development
        if not config['local']:
            config['local'] = 'hublink_dev'
        
        print(f"\nReady to restore:")
        print(f"  Source: {'Production Database' if source_choice == '2' else backup_path}")
        print(f"  Target: Local database '{config['local']}'")
        confirm = input("\nProceed with restore? (y/n): ").lower()
        if confirm != 'y':
            print("\nRestore cancelled.")
            return
        
        # Check for required PostgreSQL tools
        check_postgres_tools()
        
        # Handle file-based vs direct backup
        if source_choice == "1":
            # File-based backup
            with tempfile.TemporaryDirectory() as temp_dir:
                try:
                    print("\n=== Starting Local Database Restore Process ===")
                    
                    # Extract the backup
                    backup_dir = extract_backup(backup_path, temp_dir)
                    
                    # Restore the database
                    restore_local_database(backup_dir, config['local'])
                    
                    print("\n=== Local Database Restore Process Complete ===")
                    
                except Exception as e:
                    print(f"\nError during restore process: {e}")
                    sys.exit(1)
                finally:
                    print("\nCleaning up temporary files...")
        else:
            # Direct backup
            try:
                print("\n=== Starting Local Database Restore Process ===")
                
                # Restore directly from backup file
                restore_local_database(backup_path, config['local'])
                
                print("\n=== Local Database Restore Process Complete ===")
                
            except Exception as e:
                print(f"\nError during restore process: {e}")
                sys.exit(1)
            finally:
                # Clean up temporary backup file
                if temp_backup and os.path.exists(backup_path):
                    os.unlink(backup_path)
                    print("\nCleaned up temporary backup file")
        
    elif target_choice == "2":
        # Staging environment
        if not config['staging']:
            print("Error: No staging database configured.")
            print("Please set up your database configurations first.")
            return
        
        # Validate the URL
        is_valid, message = validate_staging_url(config['staging'])
        if not is_valid:
            print(f"Error: {message}")
            return
        
        # Test connection
        if not test_database_connection(config['staging'], "staging"):
            print("Cannot proceed without a valid connection to staging database.")
            return
        
        print(f"\nReady to restore:")
        print(f"  Source: {'Production Database' if source_choice == '2' else backup_path}")
        print(f"  Target: Staging database")
        confirm = input("\nProceed with restore? (y/n): ").lower()
        if confirm != 'y':
            print("\nRestore cancelled.")
            return
        
        # Check for required PostgreSQL tools
        check_postgres_tools()
        
        # Handle file-based vs direct backup
        if source_choice == "1":
            # File-based backup
            with tempfile.TemporaryDirectory() as temp_dir:
                try:
                    print("\n=== Starting Staging Database Restore Process ===")
                    
                    # Extract the backup
                    backup_dir = extract_backup(backup_path, temp_dir)
                    
                    # Restore the database
                    restore_staging_database(backup_dir, config['staging'])
                    
                    print("\n=== Staging Database Restore Process Complete ===")
                    
                except Exception as e:
                    print(f"\nError during restore process: {e}")
                    sys.exit(1)
                finally:
                    print("\nCleaning up temporary files...")
        else:
            # Direct backup
            try:
                print("\n=== Starting Staging Database Restore Process ===")
                
                # Restore directly from backup file
                restore_staging_database(backup_path, config['staging'])
                
                print("\n=== Staging Database Restore Process Complete ===")
                
            except Exception as e:
                print(f"\nError during restore process: {e}")
                sys.exit(1)
            finally:
                # Clean up temporary backup file
                if temp_backup and os.path.exists(backup_path):
                    os.unlink(backup_path)
                    print("\nCleaned up temporary backup file")
        
    else:
        print("Invalid choice. Please enter 1 or 2.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nRestore cancelled by user.")
        sys.exit(1) 