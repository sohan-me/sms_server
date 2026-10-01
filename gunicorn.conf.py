# "gthread" is required: the default "sync" worker handles one request at a
# time, so an open WebSocket blocks the whole worker.
# Never set preload_app = True — app.py opens the Redis subscriber at import.
import multiprocessing
import os

bind = os.environ.get("BIND", "0.0.0.0:8002")
workers = int(os.environ.get("WEB_CONCURRENCY", max(2, multiprocessing.cpu_count())))
worker_class = "gthread"
threads = int(os.environ.get("WEB_THREADS", 8))

# WebSockets are long-lived and idle; the 30s default would kill them.
timeout = int(os.environ.get("WEB_TIMEOUT", 120))
graceful_timeout = 30
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")
