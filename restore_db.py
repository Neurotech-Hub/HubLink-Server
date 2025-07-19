#!/usr/bin/env python3

import os
import sys
import subprocess
import tempfile
import shutil
from pathlib import Path
import tarfile
import re
from urllib.parse import urlparse

# Add PostgreSQL 16 tools to PATH
pg16_bin_path = "/opt/homebrew/opt/postgresql@16/bin"
if os.path.exists(pg16_bin_path):
    current_path = os.environ.get('PATH', '')
    os.environ['PATH'] = f"{pg16_bin_path}:{current_path}"

def check_postgres_tools():
    """Verify that required PostgreSQL tools are available."""
    required_tools = ['psql', 'createdb', 'dropdb', 'pg_restore']
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

def validate_staging_url(db_url):
    """Validate that the database URL is for staging environment."""
    if not db_url:
        return False, "Database URL is required"
    
    # Check for _stag in the URL (case insensitive)
    if '_stag' not in db_url.lower():
        return False, "Database URL must contain '_stag' for safety (to avoid production overwrites)"
    
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

def test_staging_connection(db_url):
    """Test connection to staging database."""
    try:
        print("Testing connection to staging database...")
        result = subprocess.run([
            "psql", db_url, "-c", "SELECT 1 as connection_test;"
        ], capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            print("✓ Connection to staging database successful")
            return True
        else:
            print(f"✗ Connection failed: {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        print("✗ Connection timeout - check your database URL and network connection")
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

def restore_local_database(backup_dir, db_name="hublink_dev"):
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
            backup_dir
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

def restore_staging_database(backup_dir, db_url):
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
            backup_dir
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
    print("This tool will restore a Render PostgreSQL backup (.dir.tar.gz) to your local database or staging environment.")
    
    # Get backup file path
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
    
    # Choose environment
    while True:
        print("\nChoose target environment:")
        print("1. Local Development")
        print("2. Staging Environment")
        
        choice = input("\nEnter your choice (1 or 2): ").strip()
        
        if choice == "1":
            # Local development
            db_name = input("\nEnter target database name (press Enter for 'hublink_dev'): ").strip()
            if not db_name:
                db_name = 'hublink_dev'
            
            print(f"\nReady to restore:")
            print(f"  Source: {backup_path}")
            print(f"  Target: Local database '{db_name}'")
            confirm = input("\nProceed with restore? (y/n): ").lower()
            if confirm != 'y':
                print("\nRestore cancelled.")
                return
            
            # Check for required PostgreSQL tools
            check_postgres_tools()
            
            # Create temporary directory for extraction
            with tempfile.TemporaryDirectory() as temp_dir:
                try:
                    print("\n=== Starting Local Database Restore Process ===")
                    
                    # Extract the backup
                    backup_dir = extract_backup(backup_path, temp_dir)
                    
                    # Restore the database
                    restore_local_database(backup_dir, db_name)
                    
                    print("\n=== Local Database Restore Process Complete ===")
                    
                except Exception as e:
                    print(f"\nError during restore process: {e}")
                    sys.exit(1)
                finally:
                    print("\nCleaning up temporary files...")
            
            break
            
        elif choice == "2":
            # Staging environment
            print("\nEnter the staging database URL (e.g., postgresql://user:pass@host/db):")
            db_url = input("Database URL: ").strip()
            
            # Validate the URL
            is_valid, message = validate_staging_url(db_url)
            if not is_valid:
                print(f"Error: {message}")
                continue
            
            # Test connection
            if not test_staging_connection(db_url):
                print("Cannot proceed without a valid connection to staging database.")
                continue
            
            print(f"\nReady to restore:")
            print(f"  Source: {backup_path}")
            print(f"  Target: Staging database")
            print(f"  URL: {db_url}")
            confirm = input("\nProceed with restore? (y/n): ").lower()
            if confirm != 'y':
                print("\nRestore cancelled.")
                return
            
            # Check for required PostgreSQL tools
            check_postgres_tools()
            
            # Create temporary directory for extraction
            with tempfile.TemporaryDirectory() as temp_dir:
                try:
                    print("\n=== Starting Staging Database Restore Process ===")
                    
                    # Extract the backup
                    backup_dir = extract_backup(backup_path, temp_dir)
                    
                    # Restore the database
                    restore_staging_database(backup_dir, db_url)
                    
                    print("\n=== Staging Database Restore Process Complete ===")
                    
                except Exception as e:
                    print(f"\nError during restore process: {e}")
                    sys.exit(1)
                finally:
                    print("\nCleaning up temporary files...")
            
            break
            
        else:
            print("Invalid choice. Please enter 1 or 2.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nRestore cancelled by user.")
        sys.exit(1) 