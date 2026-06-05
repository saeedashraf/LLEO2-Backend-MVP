"""
Google Earth Engine Authentication
Handles Earth Engine initialization with service account credentials
"""

import os
import ee
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request as GoogleRequest


def initialize_earth_engine(config=None, ee_credentials_path=None):
    """
    Initialize Google Earth Engine with service account credentials.

    Args:
        config: LLEOConfig object with ee_service_account path (optional)
        ee_credentials_path: Direct path to credentials file (optional)

    The function tries multiple credential sources in order:
    1. ee_credentials_path parameter
    2. config.ee_service_account
    3. EE_SERVICE_ACCOUNT environment variable
    4. Local file 'gen-lang-client-0709485529-7db838c888b4.json'
    5. Default ee.Initialize() (uses system credentials)
    """
    print("🌍 Initializing Google Earth Engine...")

    # Determine credentials path
    credentials_path = None

    if ee_credentials_path:
        credentials_path = ee_credentials_path
    elif config and config.ee_service_account:
        credentials_path = config.ee_service_account
    elif os.getenv('EE_SERVICE_ACCOUNT'):
        credentials_path = os.getenv('EE_SERVICE_ACCOUNT')
    else:
        # Try local file
        local_file = "gen-lang-client-0709485529-7db838c888b4.json"
        if os.path.exists(local_file):
            credentials_path = local_file
            print(f"✅ Using local Earth Engine credentials: {local_file}")

    # Initialize Earth Engine
    try:
        # Get Google Cloud project ID
        project_id = os.getenv('GOOGLE_CLOUD_PROJECT', 'gen-lang-client-0709485529')

        if credentials_path:
            # Use service account file
            credentials = Credentials.from_service_account_file(
                credentials_path,
                scopes=['https://www.googleapis.com/auth/cloud-platform']
            )

            # Refresh if expired
            if credentials.expired:
                credentials.refresh(GoogleRequest())

            ee.Initialize(credentials, project=project_id)
            print(f"✅ Google Earth Engine initialized with service account: {credentials_path}, project: {project_id}")

        else:
            # Use default credentials
            ee.Initialize(project=project_id)
            print(f"✅ Google Earth Engine initialized (default credentials), project: {project_id}")

    except Exception as e:
        print(f"❌ Error initializing Google Earth Engine: {e}")
        raise RuntimeError(f"Failed to initialize Earth Engine: {e}")
