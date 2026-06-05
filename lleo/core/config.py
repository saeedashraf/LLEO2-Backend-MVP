"""
Configuration management for LLEO package
"""

from dataclasses import dataclass
from typing import Optional, Dict, Any
import os


@dataclass
class LLEOConfig:
    """Configuration for LLEO processors"""

    # Earth Engine
    ee_service_account: Optional[str] = None
    ee_private_key: Optional[str] = None

    # Google Cloud
    gcp_project_id: Optional[str] = None
    gcp_region: Optional[str] = None
    storage_bucket: Optional[str] = None

    # Database
    database_url: Optional[str] = None
    supabase_url: Optional[str] = None
    supabase_key: Optional[str] = None

    # API Keys
    google_places_api_key: Optional[str] = None
    openweather_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None

    # Planet Labs (for video_animation / timelapse)
    # Replaces deprecated Sentinel Hub — uses Planet SDK v3
    planet_api_key: Optional[str] = None

    # Legacy Sentinel Hub (DEPRECATED – March 2026, kept for reference)
    sentinel_hub_client_id: Optional[str] = None
    sentinel_hub_client_secret: Optional[str] = None

    # Processing settings
    max_workers: int = 4
    timeout: int = 600
    temp_dir: str = "/tmp/lleo"

    # Analysis settings
    default_buffer_size: int = 2000  # meters (reduced from 5000m for faster processing)
    max_image_count: int = 50
    cloud_cover_threshold: int = 20

    @classmethod
    def from_env(cls) -> 'LLEOConfig':
        """Create config from environment variables - matches main.py configuration"""
        return cls(
            ee_service_account=os.getenv('EE_SERVICE_ACCOUNT'),
            ee_private_key=os.getenv('EE_PRIVATE_KEY'),
            # Use same environment variable names as main.py
            gcp_project_id=os.getenv('GOOGLE_CLOUD_PROJECT', 'gen-lang-client-0709485529'),
            gcp_region=os.getenv('REGION', 'us-central1'),
            storage_bucket=os.getenv('STORAGE_BUCKET_NAME', 'ndvi3-analysis-storage-bucket'),
            database_url=os.getenv('DATABASE_URL'),
            supabase_url=os.getenv('SUPABASE_URL'),
            supabase_key=os.getenv('SUPABASE_KEY'),
            google_places_api_key=os.getenv('GOOGLE_PLACES_API_KEY'),
            openweather_api_key=os.getenv('OPENWEATHER_API_KEY'),
            gemini_api_key=os.getenv('GEMINI_API_KEY'),
            planet_api_key=os.getenv('PLANET_API_KEY') or os.getenv('PL_API_KEY'),
            sentinel_hub_client_id=os.getenv('SENTINEL_HUB_CLIENT_ID'),
            sentinel_hub_client_secret=os.getenv('SENTINEL_HUB_CLIENT_SECRET'),
            max_workers=int(os.getenv('MAX_WORKERS', '4')),
            timeout=int(os.getenv('ANALYSIS_TIMEOUT', '600')),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert config to dictionary"""
        return {
            'gcp_project_id': self.gcp_project_id,
            'gcp_region': self.gcp_region,
            'storage_bucket': self.storage_bucket,
            'max_workers': self.max_workers,
            'timeout': self.timeout,
            'default_buffer_size': self.default_buffer_size,
            'max_image_count': self.max_image_count,
            'cloud_cover_threshold': self.cloud_cover_threshold,
        }
