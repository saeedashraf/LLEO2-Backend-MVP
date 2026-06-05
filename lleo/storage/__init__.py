"""
Storage Module for LLEO Package
Handles file storage in Google Cloud Storage with local fallback
"""

from .cloud_manager import CloudFileManager

__all__ = ['CloudFileManager']
