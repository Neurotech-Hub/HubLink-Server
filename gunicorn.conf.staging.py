import logging
import sys
import multiprocessing

# Binding
bind = "0.0.0.0:10000"

# Worker configuration optimized for 0.5 CPU and 512MB memory
workers = 1                  # Single worker to conserve RAM
worker_class = "sync"        # Simple synchronous worker for limited resources
threads = 2                  # Increased threading for improved CPU utilization
worker_connections = 75      # Higher concurrent connections with better CPU

# Timeout configuration
timeout = 45                 # Reduced timeout with better CPU performance
keepalive = 2
graceful_timeout = 30

# Logging configuration
accesslog = "-"
errorlog = "-"
loglevel = "warning"        # Increased logging level with better CPU
capture_output = True

# Performance tuning for 0.5 CPU and 512MB RAM
max_requests = 400          # Increased for better CPU utilization
max_requests_jitter = 40    # Proportionally increased
backlog = 64               # Increased queue size for better CPU handling

# Memory optimization for staging
worker_tmp_dir = "/dev/shm"  # Use RAM for temp files
preload_app = True          # Share application code between workers
post_fork = lambda server, worker: server.log.info("Worker spawned (pid: %s)", worker.pid)

def on_starting(server):
    # Configure root logger for staging
    root = logging.getLogger()
    root.setLevel(logging.WARNING)  # Warning level with improved CPU
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter('%(asctime)s [%(levelname)s] [%(name)s] %(message)s')
    )
    root.addHandler(handler) 