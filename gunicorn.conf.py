import logging
import sys
import multiprocessing

# Binding
bind = "0.0.0.0:10000"

# Worker configuration - optimized for 1 CPU, 2GB RAM
workers = 2  # Fixed number for 1 CPU instead of dynamic calculation
worker_class = "sync"
threads = 1
worker_connections = 250  # Reduced from 1000

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
max_requests = 500  # Reduced from 1000
max_requests_jitter = 50  # Reduced from 100
backlog = 512  # Reduced from 2048

def on_starting(server):
    # Configure root logger to use stdout
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter('%(asctime)s [%(levelname)s] [%(name)s] %(message)s')
    )
    root.addHandler(handler) 