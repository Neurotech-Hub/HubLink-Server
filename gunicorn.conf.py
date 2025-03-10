import logging
import sys
import multiprocessing

# Binding
bind = "0.0.0.0:10000"

# Worker configuration - single worker for SQLite compatibility
workers = 1  # Reduced from 2 to prevent SQLite locking issues
worker_class = "sync"
threads = 1
worker_connections = 250

# Timeout configuration
timeout = 120
keepalive = 5
graceful_timeout = 30

# Logging configuration
accesslog = "-"
errorlog = "-"
loglevel = "info"
capture_output = True

# Performance tuning - reduced for memory constraints
max_requests = 500
max_requests_jitter = 50
backlog = 512

def on_starting(server):
    # Configure root logger to use stdout
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter('%(asctime)s [%(levelname)s] [%(name)s] %(message)s')
    )
    root.addHandler(handler) 