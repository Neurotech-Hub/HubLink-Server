import logging
import sys
import multiprocessing

# Binding
bind = "0.0.0.0:10000"

# Worker configuration optimized for 1GB memory
workers = 2                  # Increased for better CPU utilization
worker_class = "sync"
threads = 4                  # Increased for better concurrent processing
worker_connections = 100     # Increased for more concurrent connections

# Timeout configuration
timeout = 30
keepalive = 2
graceful_timeout = 30

# Logging configuration
accesslog = "-"
errorlog = "-"
loglevel = "warning"
capture_output = True

# Performance tuning for higher resource environment
max_requests = 1000         # Increased since memory isn't a constraint
max_requests_jitter = 100   # Increased proportionally
backlog = 128              # Increased for more queued connections

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