"""
ChronosMatch: FastAPI ASGI Application Instance for Vercel and cloud deployments.
"""
import os
import sys

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from chronos_match.web.server import app

__all__ = ["app"]
