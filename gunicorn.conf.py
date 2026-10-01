# Gunicorn config for the OrbitalCore SMS/OTP server.
#
# worker_class must NOT be the default "sync": a sync worker handles one request
# at a time, so a single long-lived WebSocket connection blocks that worker
# indefinitely and every other client sharing it stalls. "gthread" keeps the
# existing threaded Flask code path working without an async rewrite.
#
# Do NOT set preload_app = True. app.py opens the OTP Redis subscriber at import
# time; forking workers would inherit a dead socket and no worker would receive
# fan-out messages.

import multiprocessing
import os


bind = os.environ.get("BIND", "0.0.0.0:5000")
workers = int(os.environ.get("WEB_CONCURRENCY", max(2, multiprocessing.cpu_count())))
worker_class = "gthread"
threads = int(os.environ.get("WEB_THREADS", 8))

# WebSocket connections are long-lived and mostly idle; the default 30s timeout
# would kill them.
timeout = int(os.environ.get("WEB_TIMEOUT", 120))
graceful_timeout = 30
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")
