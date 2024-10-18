from flask import Flask
from models import db, Account, Setting  # Make sure to import both models
import routes
import os

# Create Flask app with instance folder configuration
app = Flask(__name__, instance_relative_config=True)

# Ensure the instance folder exists
if not os.path.exists(app.instance_path):
    os.makedirs(app.instance_path)

# Configure the SQLite database to be inside the instance folder
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(app.instance_path, 'accounts.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize the database
db.init_app(app)

# Define the routes
app.add_url_rule('/', 'index', routes.index)
app.add_url_rule('/submit', 'submit', routes.submit, methods=['POST'])
app.add_url_rule('/dashboard/<account_url>', 'account_dashboard', routes.account_dashboard)
app.add_url_rule('/update/<int:account_id>', 'update_settings', routes.update_settings, methods=['POST'])
app.add_url_rule('/delete/<int:account_id>', 'delete_account', routes.delete_account, methods=['POST'])

# Create tables explicitly before running the app
with app.app_context():
    try:
        db.create_all()  # Create all tables (Account and Setting)
        print("Database tables created successfully.")
    except Exception as e:
        print(f"Error creating tables: {e}")

if __name__ == '__main__':
    app.run(debug=True)
