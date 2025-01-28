import logging
import sys

# Minimal Gunicorn config
bind = "0.0.0.0:10000"

# Logging configuration
accesslog = "-"  # stdout
errorlog = "-"   # stderr
loglevel = "info"
capture_output = True  # Capture stdout/stderr from workers

def on_starting(server):
    # Configure root logger to use stdout
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter('%(asctime)s [%(levelname)s] [%(name)s] %(message)s')
    )
    root.addHandler(handler) 