"""
Earth Engine Module for LLEO Package
Handles Google Earth Engine authentication and satellite data access
"""

from .auth import initialize_earth_engine

__all__ = [
    'initialize_earth_engine'
]
