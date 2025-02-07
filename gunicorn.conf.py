import logging
import sys
import multiprocessing

# Binding
bind = "0.0.0.0:10000"

# Worker configuration
workers = multiprocessing.cpu_count() * 2 + 1  # Generally accepted formula for web apps
worker_class = "sync"  # Sync workers are sufficient for SQLite
threads = 1  # Single-threaded since we're using SQLite
worker_connections = 1000

# Timeout configuration
timeout = 120  # Seconds
keepalive = 5  # Seconds
graceful_timeout = 30

# Logging configuration
accesslog = "-"  # stdout
errorlog = "-"   # stderr
loglevel = "info"
capture_output = True  # Capture stdout/stderr from workers

# Performance tuning
max_requests = 1000  # Restart workers after this many requests
max_requests_jitter = 100  # Add randomness to max_requests
backlog = 2048  # Maximum number of pending connections

def on_starting(server):
    # Configure root logger to use stdout
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter('%(asctime)s [%(levelname)s] [%(name)s] %(message)s')
    )
    root.addHandler(handler) 