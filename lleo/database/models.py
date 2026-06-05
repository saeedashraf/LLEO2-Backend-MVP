"""
Database Models for LLEO Package
SQLAlchemy models for analysis storage and history
"""

import os
from datetime import datetime
from typing import Generator
from sqlalchemy import create_engine, Column, String, DateTime, JSON, Integer, Text, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.sql import func


# Base class for models
Base = declarative_base()


# =============================================================================
# DATABASE MODELS
# =============================================================================

class AnalysisRecord(Base):
    """
    Stores analysis metadata and results.

    Fields:
    - session_id: Unique identifier for the analysis
    - user_id: User who requested the analysis
    - query: Original natural language query
    - analysis_type: Type of analysis (ndvi, air_quality, etc.)
    - location_name: Human-readable location name
    - coordinates: JSON with lat/lng bounds
    - start_year, end_year: Time period analyzed
    - created_at: Timestamp of analysis
    - files: JSON with paths to generated files
    - status: Analysis status (completed, failed, processing)
    - thumbnail_path: Path to thumbnail image
    - results_data: JSON with analysis results
    - conversation_history: JSON array of conversation messages (role, content, timestamp)
    """
    __tablename__ = "analyses"

    session_id = Column(String, primary_key=True, index=True)
    user_id = Column(String, nullable=False, index=True)
    query = Column(Text)
    analysis_type = Column(String)
    location_name = Column(String)
    coordinates = Column(JSON)
    start_year = Column(Integer)
    end_year = Column(Integer)
    created_at = Column(DateTime, default=func.now())
    files = Column(JSON)
    status = Column(String, default="completed")
    thumbnail_path = Column(String)
    thumbnail_url = Column(String)  # Direct URL for frontend
    map_url = Column(String)  # Direct URL to map.html
    chart_url = Column(String)  # Direct URL to charts.png
    results_data = Column(JSON)
    conversation_history = Column(JSON)


# =============================================================================
# DATABASE CONNECTION
# =============================================================================

def create_database_engine(config=None, get_secret_func=None):
    """
    Create SQLAlchemy engine with automatic PostgreSQL/SQLite fallback.

    Args:
        config: LLEOConfig object with database settings
        get_secret_func: Function to retrieve database password from secrets

    Returns:
        SQLAlchemy Engine instance
    """
    # Get database credentials
    db_password = None
    if get_secret_func:
        try:
            db_password = get_secret_func('ndvi3-db-password')
        except:
            pass

    if not db_password:
        db_password = os.getenv('DB_PASSWORD')

    db_user = os.getenv('DB_USER', 'ndvi3_user')
    db_name = os.getenv('DB_NAME', 'ndvi3_analysis')
    project_id = config.gcp_project_id if config else os.getenv('GCP_PROJECT_ID')

    # Determine if running in Cloud (App Engine, Cloud Run, etc.)
    is_cloud = (
        os.getenv('GAE_ENV', '').startswith('standard') or
        os.getenv('K_SERVICE') or
        os.getenv('CLOUD_RUN_SERVICE')
    )

    # Build database URL
    if is_cloud and project_id:
        # Cloud SQL with Unix socket
        cloud_sql_connection = f"{project_id}:us-central1:ndvi3-postgres-instance"
        database_url = f"postgresql://{db_user}:{db_password}@/{db_name}?host=/cloudsql/{cloud_sql_connection}"
        print(f"☁️  Using Cloud SQL: {cloud_sql_connection}")
    else:
        # Local PostgreSQL
        db_host = os.getenv('DB_HOST', 'localhost')
        database_url = f"postgresql://{db_user}:{db_password}@{db_host}:5432/{db_name}"
        print(f"🏠 Using local PostgreSQL: {db_host}")

    # Try PostgreSQL connection
    try:
        engine = create_engine(
            database_url,
            pool_pre_ping=True,
            pool_recycle=300,
            pool_timeout=30,
            pool_size=50,
            max_overflow=50
        )

        # Test connection
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        print("✅ Database connection established (PostgreSQL)")
        return engine

    except Exception as e:
        print(f"⚠️  PostgreSQL connection failed: {e}")
        print("🔄 Falling back to SQLite")

        # Fallback to SQLite
        sqlite_url = "sqlite:///./ndvi3_analyses.db"
        engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})
        print("✅ Database connection established (SQLite)")
        return engine


# Global engine and session factory (initialized by init_database())
engine = None
SessionLocal = None


def init_database(config=None, get_secret_func=None):
    """
    Initialize database engine, session factory, and create tables.

    Args:
        config: LLEOConfig object
        get_secret_func: Function to retrieve secrets

    This should be called once at application startup.
    """
    global engine, SessionLocal

    # Create engine
    engine = create_database_engine(config, get_secret_func)

    # Create session factory
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Create tables
    try:
        Base.metadata.create_all(bind=engine)
        print("✅ Database tables created/verified")
    except Exception as e:
        print(f"❌ Database table creation failed: {e}")

    # Run migrations — add columns that may be missing in existing databases
    _run_migrations(engine)


def _run_migrations(eng):
    """Add columns that may be missing in older databases."""
    migrations = [
        "ALTER TABLE analyses ADD COLUMN IF NOT EXISTS thumbnail_url VARCHAR",
        "ALTER TABLE analyses ADD COLUMN IF NOT EXISTS map_url VARCHAR",
        "ALTER TABLE analyses ADD COLUMN IF NOT EXISTS chart_url VARCHAR",
        "ALTER TABLE analyses ADD COLUMN IF NOT EXISTS conversation_history JSON",
    ]
    try:
        with eng.connect() as conn:
            for sql in migrations:
                try:
                    conn.execute(text(sql))
                except Exception:
                    pass  # SQLite doesn't support IF NOT EXISTS — skip silently
            conn.commit()
        print("✅ Database migrations applied")
    except Exception as e:
        print(f"⚠️  Migration skipped: {e}")


def get_db() -> Generator[Session, None, None]:
    """
    Get database session for dependency injection.

    Usage in FastAPI:
        @app.get("/endpoint")
        async def endpoint(db: Session = Depends(get_db)):
            # Use db here
    """
    if SessionLocal is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
