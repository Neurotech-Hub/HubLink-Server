import click
from flask.cli import with_appcontext

# Flask app initialization code here...

@app.cli.command("init-db")
@with_appcontext
def init_db_command():
    db.create_all()
    click.echo("Database initialized!")
