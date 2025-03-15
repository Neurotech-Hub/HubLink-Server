# Converting from SQLite to PostgreSQL

## Overview
This document outlines the steps required to migrate the HubLink Server application from SQLite to PostgreSQL, performing the migration locally and then uploading to production.

## Current Status
- ✅ Added psycopg2-binary to requirements.txt
- ✅ PostgreSQL installation verified and working
- ✅ Created hublink_dev database
- ✅ Added database configuration to .env
- ✅ Updated database configuration in app.py
- ✅ Updated models for PostgreSQL compatibility
- ✅ Added timezone support to DateTime fields
- ✅ Changed Integer to BigInteger for large sizes
- ✅ Switched to JSONB for JSON fields
- ✅ Fixed boolean default values
- ✅ Removed SQLite-specific default values
- ✅ Created data migration script
- ✅ Executed data migration successfully
- ✅ Verified data integrity post-migration
- ✅ Fixed JSON/JSONB handling in application code
- ⏳ Pending production deployment on Render

## Version Compatibility Note
⚠️ Important: Use PostgreSQL 16 locally to match Render's version. While PostgreSQL 17 works locally, it can cause issues when migrating to production since pg_dump cannot reliably dump to older versions.

## Local Development Setup
1. Install PostgreSQL:
   ```bash
   # Download PostgreSQL.app from https://postgresapp.com/
   # Important: Download version 16, not 17
   
   # Add to PATH in ~/.zshrc (update path to use version 16):
   export PATH="/Applications/Postgres.app/Contents/Versions/16/bin:$PATH"
   ```

2. Create local database:
Enter PostgreSQL editor via `psql`.
   ```sql
   -- First, verify you're running PostgreSQL 16
   SELECT version();
   
   -- Then create the database
   CREATE DATABASE hublink_dev;
   ```

3. Configure environment:
   ```bash
   # Update .env
   DATABASE_URL=postgresql://localhost/hublink_dev
   ```

## Local to Production Migration Steps

### 1. Prepare Local Environment
```bash
# First, verify PostgreSQL version
psql --version  # Should show PostgreSQL 16.x

# Drop and recreate local database (fresh start)
psql postgres -c "DROP DATABASE IF EXISTS hublink_dev;"
psql postgres -c "CREATE DATABASE hublink_dev;"

# Install requirements
pip install -r requirements.txt

# Run migrations and data migration
python migrate_data.py  # This script handles:
                       # - SQLite backup
                       # - Schema creation (flask db upgrade)
                       # - Table migration order for foreign keys
                       # - Data type conversions
                       # - JSON/JSONB handling
                       # - Sequence updates
                       # - Transaction safety

# Verify local migration
psql hublink_dev -c "SELECT COUNT(*) FROM account;"
psql hublink_dev -c "SELECT COUNT(*) FROM file;"
psql hublink_dev -c "SELECT COUNT(*) FROM source;"
```

### 2. Create Production Database Dump (Run Locally)
```bash
# Create a complete dump with schema and data
pg_dump --no-owner --no-acl --clean --if-exists \
  hublink_dev > backups/hublink_migration.sql

# Note: Now that we've cleaned orphaned records and added CASCADE constraints,
# we can use a simple dump without complex ordering
```

### 3. Restore to Production (Run Locally)
```bash
# 1. Connect to production database
render psql

# 2. Once connected, run:
BEGIN;
\set ON_ERROR_STOP on
\echo 'Starting restore...'
\i backups/hublink_migration.sql
COMMIT;
\echo 'Restore complete'

# 3. Verify the restore
\dt
SELECT COUNT(*) FROM account;
SELECT COUNT(*) FROM file;
SELECT COUNT(*) FROM source;
```

### Important Notes
1. **Version Compatibility**: Ensure you're using PostgreSQL 16 locally to match Render's version
2. **Data Integrity**: The migration script will:
   - Clean orphaned records before migration
   - Verify foreign key constraints
   - Handle data type conversions (especially for JSONB fields)
3. **Cascading Deletes**: All relationships are set up with `ondelete='CASCADE'` where appropriate
4. **Transaction Safety**: Each table migration is wrapped in its own transaction
5. **Error Handling**: The script includes comprehensive error logging and rollback capabilities

### Troubleshooting
If you encounter issues:
1. Check the migration.log file for detailed error messages
2. Verify PostgreSQL version compatibility
3. Ensure all tables are properly created before data migration
4. Check for any orphaned records in the source database