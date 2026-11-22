#!/usr/bin/env python3
"""Sandbox helper: run uvicorn with a self-destruct timer.

Sandboxes that block signals make `kill` unusable; this wrapper exits the
process by itself after TTL seconds so test drivers can bounce the server.
Not used in production.
"""
import os
import sys
import threading

import uvicorn

port = int(sys.argv[1])
ttl = int(sys.argv[2])
app = sys.argv[3] if len(sys.argv) > 3 else "app.main:app"

timer = threading.Timer(ttl, lambda: os._exit(0))
timer.daemon = True
timer.start()
uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
