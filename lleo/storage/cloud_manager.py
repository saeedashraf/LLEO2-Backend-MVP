"""
Cloud File Manager
Handles file storage in Google Cloud Storage with local fallback
"""

import os
import tempfile
import time
from typing import Optional
from google.cloud import storage


class CloudFileManager:
    """
    Manages file storage in Google Cloud Storage with automatic local fallback.

    Features:
    - Uploads files to GCS bucket
    - Downloads files from GCS
    - Local storage fallback when GCS is unavailable
    - File existence checks
    - File deletion
    """

    def __init__(self, config=None, bucket_name: Optional[str] = None):
        """
        Initialize CloudFileManager.

        Args:
            config: LLEOConfig object with storage_bucket
            bucket_name: Direct bucket name (optional, overrides config)
        """
        # Determine bucket name
        if bucket_name:
            self.bucket_name = bucket_name
        elif config and config.storage_bucket:
            self.bucket_name = config.storage_bucket
        else:
            self.bucket_name = os.getenv('STORAGE_BUCKET')

        # Try to initialize Cloud Storage client
        self.bucket = None
        self.use_cloud = False

        if self.bucket_name:
            try:
                from google.oauth2.service_account import Credentials

                # Try to find service account credentials
                credentials = None

                # Check local file in lleo directory
                local_creds_path = os.path.join(os.path.dirname(__file__), '..', 'gen-lang-client-0709485529-7db838c888b4.json')
                if os.path.exists(local_creds_path):
                    credentials = Credentials.from_service_account_file(local_creds_path)
                    storage_client = storage.Client(credentials=credentials)
                    print(f"☁️  Using Cloud Storage with service account: {local_creds_path}")
                else:
                    # On Cloud Run, use default credentials
                    storage_client = storage.Client()
                    print(f"☁️  Using Cloud Storage with default credentials")

                self.bucket = storage_client.bucket(self.bucket_name)
                self.use_cloud = True

                # Set CORS configuration to allow frontend access
                self._setup_cors()
            except Exception as e:
                print(f"⚠️  Cloud Storage unavailable: {e}")
                import traceback
                traceback.print_exc()
                self.use_cloud = False

        # Set up local storage fallback
        if not self.use_cloud:
            print("⚠️  Using local file storage")
            self.local_results_dir = "analysis_results"
            os.makedirs(self.local_results_dir, exist_ok=True)
        else:
            self.local_results_dir = "analysis_results"  # Still need for temp files

    def _setup_cors(self):
        """Configure CORS to allow frontend access to GCS files."""
        try:
            cors_config = [
                {
                    "origin": ["https://lleo.earth", "http://localhost:3000", "http://localhost:5173"],
                    "method": ["GET", "HEAD"],
                    "responseHeader": ["Content-Type"],
                    "maxAgeSeconds": 3600
                }
            ]
            self.bucket.cors = cors_config
            self.bucket.patch()
            print(f"✅ CORS configured for bucket: {self.bucket_name}")
        except Exception as e:
            print(f"⚠️  Could not set CORS (may not have permission, but files may still work): {e}")

    def save_file_to_cloud(self, local_path: str, cloud_path: str) -> str:
        """
        Upload a file to Cloud Storage.

        Args:
            local_path: Path to local file
            cloud_path: Destination path in GCS bucket (without gs:// prefix)

        Returns:
            str: GCS URL (gs://bucket/path) or local path if cloud unavailable
        """
        if not self.use_cloud:
            return local_path

        try:
            blob = self.bucket.blob(cloud_path)
            blob.upload_from_filename(local_path)
            cloud_url = f"gs://{self.bucket_name}/{cloud_path}"
            print(f"📤 Uploaded: {local_path} -> {cloud_url}")
            return cloud_url
        except Exception as e:
            print(f"❌ Error uploading {local_path}: {e}")
            return local_path

    def get_signed_url(self, cloud_path: str, expiration_hours: int = 24) -> str:
        """
        Generate a signed URL for temporary authenticated access.

        Args:
            cloud_path: GCS URL (gs://bucket/path) or blob name
            expiration_hours: How long the URL should be valid (default 24 hours)

        Returns:
            str: Signed URL that provides temporary access
        """
        if not self.use_cloud:
            return cloud_path

        try:
            from datetime import timedelta

            # Extract blob name from gs:// URL if needed
            if cloud_path.startswith('gs://'):
                blob_name = cloud_path.replace(f"gs://{self.bucket_name}/", "")
            else:
                blob_name = cloud_path

            blob = self.bucket.blob(blob_name)

            # Generate signed URL valid for specified hours
            signed_url = blob.generate_signed_url(
                version="v4",
                expiration=timedelta(hours=expiration_hours),
                method="GET"
            )

            return signed_url
        except Exception as e:
            print(f"❌ Error generating signed URL for {cloud_path}: {e}")
            # Return original path as fallback
            return cloud_path.replace('gs://', 'https://storage.googleapis.com/')

    def get_file_from_cloud(self, cloud_path: str, local_path: Optional[str] = None) -> str:
        """
        Download a file from Cloud Storage.

        Args:
            cloud_path: GCS URL (gs://bucket/path) or blob name
            local_path: Where to save locally (creates temp file if None)

        Returns:
            str: Path to downloaded local file
        """
        if not self.use_cloud or not cloud_path.startswith('gs://'):
            return cloud_path

        try:
            blob_name = cloud_path.replace(f"gs://{self.bucket_name}/", "")
            blob = self.bucket.blob(blob_name)

            if local_path is None:
                temp_file = tempfile.NamedTemporaryFile(delete=False)
                local_path = temp_file.name
                temp_file.close()

            blob.download_to_filename(local_path)
            print(f"📥 Downloaded: {cloud_path} -> {local_path}")
            return local_path
        except Exception as e:
            print(f"❌ Error downloading {cloud_path}: {e}")
            return cloud_path

    def get_file_bytes_from_cloud(self, cloud_path: str) -> bytes:
        """
        Download file content as bytes.

        Args:
            cloud_path: GCS URL (gs://bucket/path) or local path

        Returns:
            bytes: File content as bytes
        """
        if not self.use_cloud or not cloud_path.startswith('gs://'):
            try:
                with open(cloud_path, 'rb') as f:
                    return f.read()
            except Exception as e:
                print(f"❌ Error reading local file {cloud_path}: {e}")
                return b""

        blob_name = cloud_path.replace(f"gs://{self.bucket_name}/", "")
        # Transient GCS/network errors (connection reset, 5xx, SSL EOF) used to
        # surface as a 500 for a single card thumbnail, which the UI then
        # latched as "failed" until a page reload.  Retry a couple of times
        # with a short back-off before giving up.
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                blob = self.bucket.blob(blob_name)
                return blob.download_as_bytes()
            except Exception as e:
                is_missing = type(e).__name__ == "NotFound" or "404" in str(e)
                if is_missing or attempt == attempts:
                    print(f"❌ Error downloading {cloud_path} as bytes (attempt {attempt}/{attempts}): {e}")
                    return b""
                time.sleep(0.25 * attempt)
        return b""

    def file_exists(self, path: str) -> bool:
        """
        Check if file exists in cloud or local storage.

        Args:
            path: GCS URL (gs://bucket/path) or local path

        Returns:
            bool: True if file exists
        """
        if not self.use_cloud or not path.startswith('gs://'):
            return os.path.exists(path)

        try:
            blob_name = path.replace(f"gs://{self.bucket_name}/", "")
            blob = self.bucket.blob(blob_name)
            return blob.exists()
        except:
            return False

    def delete_file(self, path: str) -> bool:
        """
        Delete file from cloud or local storage.

        Args:
            path: GCS URL (gs://bucket/path) or local path

        Returns:
            bool: True if file was deleted successfully
        """
        if not self.use_cloud or not path.startswith('gs://'):
            try:
                if os.path.exists(path):
                    os.remove(path)
                    return True
            except Exception as e:
                print(f"❌ Error deleting local file {path}: {e}")
            return False

        try:
            blob_name = path.replace(f"gs://{self.bucket_name}/", "")
            blob = self.bucket.blob(blob_name)
            blob.delete()
            print(f"🗑️  Deleted: {path}")
            return True
        except Exception as e:
            print(f"❌ Error deleting cloud file {path}: {e}")
            return False

    def list_files(self, prefix: str = "") -> list:
        """
        List files in cloud storage with given prefix.

        Args:
            prefix: Prefix to filter files (e.g., "analysis_results/")

        Returns:
            list: List of file paths
        """
        if not self.use_cloud:
            local_dir = os.path.join(self.local_results_dir, prefix)
            if os.path.exists(local_dir):
                return [os.path.join(local_dir, f) for f in os.listdir(local_dir)]
            return []

        try:
            blobs = self.bucket.list_blobs(prefix=prefix)
            return [f"gs://{self.bucket_name}/{blob.name}" for blob in blobs]
        except Exception as e:
            print(f"❌ Error listing files: {e}")
            return []
