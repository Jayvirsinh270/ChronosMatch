"""
FluxCore: FastAPI ASGI Application Instance for Vercel and cloud deployments.
"""
import os
import sys

# Unconditionally mark serverless
os.environ["VERCEL"] = "1"
os.environ["IS_SERVERLESS"] = "1"

# Ensure repository root is on sys.path
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from fluxcore.web.server import app

__all__ = ["app"]
