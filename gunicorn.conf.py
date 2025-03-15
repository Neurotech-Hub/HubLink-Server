import logging
import sys
import multiprocessing

# Binding
bind = "0.0.0.0:10000"

# Worker configuration for PostgreSQL with 1CPU 2GB constraints
workers = 2  # (1 CPU * 2) + 1 would be 3, but keeping 2 for memory constraints
worker_class = "sync"
threads = 4  # Increased from 1 since PostgreSQL can handle concurrent connections
worker_connections = 100  # Reduced from 250 to be more memory-conscious

# Timeout configuration
timeout = 120
keepalive = 5
graceful_timeout = 30

# Logging configuration
accesslog = "-"
errorlog = "-"
loglevel = "info"
capture_output = True

# Performance tuning for PostgreSQL
max_requests = 1000  # Increased since we don't have SQLite locking issues
max_requests_jitter = 100  # Increased jitter to prevent all workers restarting simultaneously
backlog = 256  # Reduced from 512 to be more memory-conscious

def on_starting(server):
    # Configure root logger to use stdout
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter('%(asctime)s [%(levelname)s] [%(name)s] %(message)s')
    )
    root.addHandler(handler) 