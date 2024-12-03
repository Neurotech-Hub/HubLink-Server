# Database Migration Workflow

This document provides a detailed overview of the full workflow for handling database migrations in this Flask application using Flask-Migrate and Alembic. By following this guide, you can effectively manage and maintain database schema changes across environments.

### Prerequisites
1. **Flask-Migrate** and **Flask-SQLAlchemy** must be installed. You can install them via:
   ```bash
   pip install Flask-Migrate Flask-SQLAlchemy
   ```
2. **Initialize your application** to use `Flask-Migrate` and `SQLAlchemy`.

### Migration Workflow

#### Step 1: Set Up Flask-Migrate (One-Time Setup)
This step should be done once when you initially create the database for your project.

1. **Import and Initialize Migrate in your Flask app**:
   ```python
   from flask import Flask
   from flask_sqlalchemy import SQLAlchemy
   from flask_migrate import Migrate
   import os

   app = Flask(__name__)
   DATABASE_PATH = os.getenv('DATABASE_PATH', '/var/www/data/accounts.db')
   app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DATABASE_PATH}'
   app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

   db = SQLAlchemy(app)
   migrate = Migrate(app, db)
   ```

2. **Create the migrations directory**:
   You need to initialize the migrations environment. Run this command from the root directory of your project:
   ```bash
   flask db init
   ```
   This will create a `migrations/` folder, which will store all the migration files and keep track of the schema changes.

#### Step 2: Make Model Changes
- Update your models in your `models.py` or wherever you’ve defined them in your Flask app.
- For example, adding a new column or changing an existing model:
  ```python
  class Account(db.Model):
      id = db.Column(db.Integer, primary_key=True)
      name = db.Column(db.String(80), unique=True, nullable=False)
      url = db.Column(db.String(120), unique=True, nullable=False)
      # New field added
      email = db.Column(db.String(120), unique=True, nullable=True)
  ```

#### Step 3: Generate a Migration Script
- To generate a new migration file that detects changes made to the models, run:
  ```bash
  flask db migrate -m "Add email field to Account model"
  ```
  This command creates a new migration script in the `migrations/versions/` directory. The `-m` flag adds a description to help you track the purpose of the migration.

#### Step 4: Apply the Migration to Update the Database
- Apply the migration to the database by running:
  ```bash
  flask db upgrade
  ```
  This command runs the migration script and applies the necessary changes to your database (e.g., adding new columns).

#### Step 5: Verify the Changes
- After running the migration, verify that the database has the updated schema.
- You can do this by inspecting your database manually or by running an interactive Python shell to check the table structure.

### Common Migration Commands Summary

- **Initialize Migrations (One-time setup)**:
  ```bash
  flask db init
  ```
- **Generate Migration Scripts (After model changes)**:
  ```bash
  flask db migrate -m "Your message describing the changes"
  ```
  This creates a new migration script, which is essentially a diff of changes made to the database schema.

- **Apply the Migration to Database**:
  ```bash
  flask db upgrade
  ```
  Applies all migrations to bring the database schema up to date.

- **Roll Back to a Previous Version (Optional)**:
  If something goes wrong and you need to roll back the last migration, you can use:
  ```bash
  flask db downgrade
  ```
  This undoes the most recent migration.

- **Check Current Database Version**:
  To see what version your database is currently at:
  ```bash
  flask db current
  ```

### Migration Best Practices

1. **Test Migrations Locally**: Always test your migrations on a local copy before applying them in production.
2. **Backup Production Database**: Before performing migrations in a production environment, make sure to take a backup of your database to avoid accidental data loss.
3. **Use Version Control for Migration Scripts**: Always commit your migration scripts to version control. This helps track changes and enables collaboration.
4. **Review Auto-generated Migrations**: Sometimes, Flask-Migrate may miss some subtle differences or generate incorrect changes. Always review the generated migration scripts before applying them to ensure they make the correct modifications.

### Deployment Workflow

1. **Update Code**: Deploy your updated application code with the updated models and migration scripts.
2. **Run Migrations on Server**:
   - SSH into your server.
   - Make sure the virtual environment is activated.
   - Navigate to your project root directory and run:
     ```bash
     flask db upgrade
     ```
   This will apply the latest migrations and bring the production database schema up to date.

### Example Workflow Recap
1. Modify your models in Python.
2. Run `flask db migrate -m "Describe your changes"`.
3. Run `flask db upgrade` to apply those changes to your database.
4. Commit the migration files in `migrations/` to your version control.

Using this workflow, you can effectively maintain database schema compatibility across development, staging, and production environments while ensuring data persistence.

### S3 Browsing Tools
- [AwsDash - simple yet powerful](https://awsdash.com/)

## Purging CSS
```bash
purgecss --css static/css/style.css --content templates/**/*.html --output static/css/style.cleaned.css
```

---

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt

### Render Config
Disks > /var/data
Environment > DATABASE_URL = sqlite:////var/data/accounts.db