"""Gunicorn settings for the ABI Render web service.

Why this file exists
--------------------
Render ran ``gunicorn 'app:create_app()'`` with no configuration: one default
*sync* worker and one thread. Every gallery image is slightly expensive (the
database is remote Turso), so a normal shop page — twelve photos — queued ahead
of staff screens and the health check behind a single synchronous worker.

Four ``gthread`` threads give bounded, in-process concurrency without a second
worker process: one worker keeps the starter plan's memory footprint where it
was, while a slow media read no longer blocks every other request. Requests,
not connections, are what parallelise here.

Database safety
---------------
``get_db()`` stores the connection on Flask's ``g``, which is per request
context. Under gthread each concurrent request runs in its own context and
therefore gets its own sqlite3/libSQL connection, closed by ``close_db`` in that
same thread. No connection is shared across threads, so sqlite3's default
``check_same_thread=True`` stays valid.

The timeout is deliberately bounded (not raised to paper over a slow request):
a request that cannot finish in 45 seconds is a fault worth logging, and
``graceful_timeout`` keeps a restart within Render's shutdown window.
"""

import os

bind = "0.0.0.0:%s" % os.environ.get("PORT", "10000")

# One process: the free/starter plan's RAM is the constraint, and the app is
# I/O-bound on the remote database, not CPU-bound.
workers = 1
threads = 4
worker_class = "gthread"

# A stuck request must fail visibly; threads are recycled with the worker.
timeout = 45
graceful_timeout = 30
keepalive = 5

# Respect the proxy in front of Render for client addresses. The app also
# applies ProxyFix for rate limiting; forwarding this header is harmless and
# keeps access logs meaningful.
forwarded_allow_ips = "*"

accesslog = "-"
errorlog = "-"
loglevel = "info"

# Do not load the app in the master before the fork: the app factory runs
# migrations on boot, and doing that once per worker (there is only one) in the
# child keeps the startup path identical to the current deployment.
preload_app = False
