import logging
import sys
import multiprocessing

# Binding
bind = "0.0.0.0:10000"

# Worker configuration optimized for 256MB memory and 0.1 CPU
workers = 1  # Single worker due to very limited CPU
worker_class = "sync"
threads = 2  # Reduced from 4 to limit memory usage
worker_connections = 50  # Reduced from 100 for memory constraints

# Timeout configuration
timeout = 30  # Reduced from 120 to fail faster
keepalive = 2  # Reduced from 5 to free up resources faster
graceful_timeout = 30

# Logging configuration
accesslog = "-"
errorlog = "-"
loglevel = "warning"  # Changed from info to reduce log volume
capture_output = True

# Performance tuning for low resource environment
max_requests = 500  # Reduced from 1000 to recycle workers more frequently
max_requests_jitter = 50  # Reduced jitter proportionally
backlog = 64  # Reduced from 256 for memory constraints

# Memory optimization
worker_tmp_dir = "/dev/shm"  # Use RAM for temp files
post_fork = lambda server, worker: server.log.info("Worker spawned (pid: %s)", worker.pid)

def on_starting(server):
    # Configure root logger to use stdout
    root = logging.getLogger()
    root.setLevel(logging.WARNING)  # Changed from INFO to reduce log volume
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter('%(asctime)s [%(levelname)s] [%(name)s] %(message)s')
    )
    root.addHandler(handler) 