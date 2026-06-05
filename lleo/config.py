"""
LLEO v2 — Centralized configuration & service initialisation.

Provides a singleton AppServices object that holds every shared
resource (Gemini model, file manager, secret client, etc.)
so routers / services import ``services`` from this module.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import warnings
from typing import Optional

from dotenv import load_dotenv

warnings.filterwarnings("ignore")
load_dotenv()

# Re-export the existing dataclass so the rest of the app still works
from .core.config import LLEOConfig  # noqa: E402


def _clean_secret(val: str) -> str:
    """Strip shell artifacts like  echo -n "...",  surrounding quotes, etc."""
    val = val.strip()
    # Remove leading  -n  (from echo -n)
    val = re.sub(r'^-n\s+', '', val)
    # Remove surrounding single or double quotes
    if len(val) >= 2 and val[0] in ('"', "'") and val[-1] == val[0]:
        val = val[1:-1]
    return val.strip()


class AppServices:
    """
    Central holder for every initialised service.
    Populated once in the FastAPI startup event.
    """

    def __init__(self) -> None:
        self.config: Optional[LLEOConfig] = None
        self.gemini_model = None
        self.file_manager = None
        self.secret_client = None
        self.analysis_sessions: dict = {}  # in-memory session store

    # ── Secrets ────────────────────────────────────────────────────────────

    def get_secret(self, secret_name: str) -> Optional[str]:
        """Retrieve a secret — env-var → Secret Manager → fallback."""
        fallback_secrets = {
            "ndvi3-db-password": "Ndvi3Secure2024!",
            "earth-engine-key": None,
            "openweather-api-key": None,
            "google-places-key": None,
            "ndvi3-admin-key": "ndvi3-admin-secure-2024",
        }

        # 1. Environment variable
        env_key = secret_name.upper().replace("-", "_")
        env_value = os.getenv(env_key)
        if env_value:
            return _clean_secret(env_value)

        # 2. Secret Manager
        if self.secret_client and self.config and self.config.gcp_project_id:
            try:
                path = (
                    f"projects/{self.config.gcp_project_id}"
                    f"/secrets/{secret_name}/versions/latest"
                )
                resp = self.secret_client.access_secret_version(
                    request={"name": path}
                )
                raw = resp.payload.data.decode("UTF-8")
                return _clean_secret(raw)
            except Exception as exc:
                if "project None" not in str(exc):
                    print(f"⚠️  Secret Manager failed for {secret_name}: {exc}")

        # 3. Fallback
        val = fallback_secrets.get(secret_name)
        return _clean_secret(val) if val else None

    # ── Master initialisation ─────────────────────────────────────────────

    async def initialise(self) -> None:
        """Called once from the FastAPI startup event."""
        print("🚀 Starting LLEO v2 — Large Language Models for Earth Observation")

        self.config = LLEOConfig.from_env()
        print(f"   Project : {self.config.gcp_project_id}")
        print(f"   Region  : {self.config.gcp_region}")
        print(f"   Bucket  : {self.config.storage_bucket}")

        self._init_secret_manager()
        self._init_gemini()
        self._init_earth_engine()
        self._init_database()
        self._init_file_manager()

        from .processors import PROCESSORS
        print(f"✅ LLEO v2 ready — {len(PROCESSORS)} processors loaded")
        for p in PROCESSORS:
            print(f"   • {p}")

    # ── Individual init helpers ───────────────────────────────────────────

    def _init_secret_manager(self) -> None:
        try:
            from google.cloud import secretmanager
            self.secret_client = secretmanager.SecretManagerServiceClient()
            print("✅ Secret Manager initialised")
        except Exception as exc:
            print(f"⚠️  Secret Manager not available: {exc}")

    def _init_gemini(self) -> None:
        try:
            import vertexai
            from vertexai.preview.generative_models import GenerativeModel
            from google.oauth2.service_account import Credentials
            from google.auth.transport.requests import Request as GoogleRequest

            creds_data = self._load_credentials_json()
            if not creds_data:
                print("⚠️  No Vertex AI credentials found")
                return

            project_id = creds_data.get("project_id", self.config.gcp_project_id)

            tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
            json.dump(creds_data, tmp)
            tmp.close()

            creds = Credentials.from_service_account_file(
                tmp.name,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            if creds.expired:
                creds.refresh(GoogleRequest())

            vertexai.init(
                project=project_id,
                location=self.config.gcp_region,
                credentials=creds,
            )

            for model_name in (
                "gemini-3-pro",
                "gemini-2.5-pro",
                "gemini-2.5-flash",
                "gemini-2.0-flash",
            ):
                try:
                    candidate = GenerativeModel(model_name)
                    candidate.generate_content(
                        "ping",
                        generation_config={"max_output_tokens": 5},
                    )
                    self.gemini_model = candidate
                    print(f"✅ Vertex AI → {model_name}")
                    break
                except Exception as e:
                    print(f"   ⏭️  {model_name}: {str(e)[:80]}")
                    continue

            os.unlink(tmp.name)

            if self.gemini_model is None:
                print("⚠️  All Gemini model candidates failed")

        except Exception as exc:
            print(f"⚠️  Vertex AI init failed: {exc}")

    def _init_earth_engine(self) -> None:
        try:
            from .earth_engine.auth import initialize_earth_engine
            initialize_earth_engine(self.config)
            print("✅ Earth Engine initialised")
        except Exception as exc:
            print(f"⚠️  Earth Engine init failed: {exc}")

    def _init_database(self) -> None:
        try:
            from .database.models import init_database
            init_database(self.config, get_secret_func=self.get_secret)
            print("✅ Database initialised")
        except Exception as exc:
            print(f"⚠️  Database init failed: {exc}")

    def _init_file_manager(self) -> None:
        try:
            from .storage.cloud_manager import CloudFileManager
            self.file_manager = CloudFileManager(self.config)
            print("✅ File manager initialised")
        except Exception as exc:
            print(f"⚠️  File manager init failed: {exc}")

    # ── Credential loader ─────────────────────────────────────────────────

    def _load_credentials_json(self) -> Optional[dict]:
        raw = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
        if raw:
            return json.loads(raw)

        # Directory containing this file (lleo/) → parent is satellite-backend-2/
        pkg_dir = os.path.dirname(os.path.abspath(__file__))
        backend_dir = os.path.dirname(pkg_dir)

        cred_file = "gen-lang-client-0709485529-7db838c888b4.json"
        for path in (
            self.config.ee_service_account if self.config else None,
            cred_file,                              # CWD-relative
            os.path.join(backend_dir, cred_file),   # satellite-backend-2/
            os.path.join(pkg_dir, cred_file),       # lleo/
        ):
            if path and os.path.exists(path):
                print(f"   Using credentials: {path}")
                with open(path) as f:
                    return json.load(f)

        return None


# Module-level singleton — import from anywhere:
#   from ..config import services
services = AppServices()
