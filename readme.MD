python3 -m venv venv
source venv/bin/activate

pip install flask
pip3 install Flask-SQLAlchemy
pip install Flask-Migrate
pip install boto3
pip install awscli

pip install -r requirements.txt

rm instance/accounts.db

Initialize Migrations:
flask db init

Create Migration Script: Whenever you make changes to your models, create a migration script:
flask db migrate -m "Initial migration."

Apply Migrations: Apply these migrations to the database:
flask db upgrade
