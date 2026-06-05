"""
LLEO v2 — Entry point.

Usage:
    uvicorn main:app --host 0.0.0.0 --port 8080
"""

from lleo.app import app  # noqa: F401
