import logging
import sys
import multiprocessing

# Binding
bind = "0.0.0.0:10000"

# Worker configuration optimized for 0.1 CPU and 512MB memory
workers = 1                  # Single worker for 0.1 CPU
worker_class = "sync"        # Simple synchronous worker for limited resources
threads = 1                  # Minimal threading due to CPU constraint
worker_connections = 25      # Very limited connections for 0.1 CPU

# Timeout configuration
timeout = 60                 # Increased timeout for slower processing
keepalive = 2
graceful_timeout = 30

# Logging configuration
accesslog = "-"
errorlog = "-"
loglevel = "error"          # Reduced logging to save resources
capture_output = True

# Performance tuning for limited resources
max_requests = 200          # Much lower to prevent memory buildup
max_requests_jitter = 20    # Proportionally reduced
backlog = 32               # Reduced queue size

# Memory optimization for staging
worker_tmp_dir = "/dev/shm"  # Use RAM for temp files
preload_app = True          # Share application code between workers
post_fork = lambda server, worker: server.log.info("Worker spawned (pid: %s)", worker.pid)

def on_starting(server):
    # Configure root logger with minimal output
    root = logging.getLogger()
    root.setLevel(logging.ERROR)  # Only log errors in staging
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter('%(asctime)s [%(levelname)s] [%(name)s] %(message)s')
    )
    root.addHandler(handler) 