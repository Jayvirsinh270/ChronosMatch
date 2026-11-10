"""
Vercel Serverless Entrypoint for FluxCore FastAPI Application.
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
os.environ.setdefault("FLUX_DB_PATH", "/tmp/fluxcore_market.db")

# Import the FastAPI application instance
from fluxcore.web.server import app
