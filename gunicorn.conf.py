# "gthread" is required: the default "sync" worker handles one request at a
# time, so an open WebSocket blocks the whole worker.
# Never set preload_app = True — app.py opens the Redis subscriber at import.
import multiprocessing
import os

bind = os.environ.get("BIND", "0.0.0.0:8002")
workers = int(os.environ.get("WEB_CONCURRENCY", max(2, multiprocessing.cpu_count())))
worker_class = "gthread"
threads = int(os.environ.get("WEB_THREADS", 8))

# CAPACITY: an open WebSocket blocks one thread for the life of the connection
# (the handler sits in ws.receive() until the peer goes away), and those same
# threads serve POST /api/messages. An idle socket holds no database connection —
# the handler releases its session after the token check — so threads, not the
# SQLAlchemy pool, are the limit.
#
# Size WEB_THREADS by the peak socket count, NOT workers * threads: gunicorn does
# not spread long-lived connections evenly. Measured by opening sockets until the
# server stopped accepting: 1x64 accepted 64; 3x16 accepted 16 (one worker took
# every connection, the other two sat idle); 3x32 accepted 59 (27 / 16 / 16).
# Extra workers buy ingest throughput only, so keep WEB_THREADS above the sockets
# you expect on one worker. Budget 3 sockets per user (one per channel) plus
# headroom: sockets that fill every thread leave none for POST /api/messages.
#
# "timeout" does NOT govern WebSockets. gunicorn's liveness check only touches a
# temp file from the worker's main loop (workers/workertmp.py), which keeps
# running no matter how many pool threads are stuck — so a worker holding dead
# WebSocket threads is never recycled. What actually keeps a long-lived WebSocket
# alive is nginx's `proxy_read_timeout 3600s`. This timeout only bounds ordinary
# request handling; keep it modest.
timeout = int(os.environ.get("WEB_TIMEOUT", 120))
graceful_timeout = 30
keepalive = 5

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")
