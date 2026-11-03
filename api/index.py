"""
Vercel Serverless Entrypoint for ChronosMatch FastAPI Application.
Routes all incoming HTTP requests and API endpoints to the FastAPI application.
"""

import os
import sys

# Ensure repository root is on Python sys.path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Identify Vercel serverless environment
os.environ["VERCEL"] = "1"
os.environ.setdefault("CHRONOS_DB_PATH", "/tmp/chronos_market.db")

# Import the FastAPI application instance
from chronos_match.web.server import app
