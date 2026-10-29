"""
Database Models for LLEO Package
SQLAlchemy models for analysis storage and history
"""

import os
from datetime import datetime
from typing import Generator
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.sql import func


# Base class for models
Base = declarative_base()

# Credits granted to every user the first time the backend sees them.
# (Replaces the Supabase `on_auth_user_created` trigger.)
SIGNUP_CREDITS = int(os.getenv("LLEO_SIGNUP_CREDITS", "20"))


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
    - user_email / conversation_id / is_shared / shared_at / likes: fields that
      used to live only in the Supabase `analyses` copy (My Analyses + Community).
    """
    __tablename__ = "analyses"

    session_id = Column(String, primary_key=True, index=True)
    user_id = Column(String, nullable=False, index=True)
    user_email = Column(String)
    query = Column(Text)
    analysis_type = Column(String)
    location_name = Column(String)
    location_lat = Column(Float)
    location_lng = Column(Float)
    coordinates = Column(JSON)
    start_year = Column(Integer)
    end_year = Column(Integer)
    parameters = Column(JSON)
    created_at = Column(DateTime, default=func.now())
    files = Column(JSON)
    status = Column(String, default="completed")
    thumbnail_path = Column(String)
    thumbnail_url = Column(String)  # Direct URL for frontend
    map_url = Column(String)  # Direct URL to map.html
    chart_url = Column(String)  # Direct URL to charts.png
    results_data = Column(JSON)
    conversation_history = Column(JSON)
    # Conversation this analysis belongs to (chat session id, "chat_..." stripped)
    conversation_id = Column(String, index=True)
    processing_time_seconds = Column(Float)
    # Community sharing
    is_shared = Column(Boolean, default=False, nullable=False, server_default=text("false"))
    shared_at = Column(DateTime)
    likes = Column(Integer, default=0, nullable=False, server_default=text("0"))


class AnalysisLike(Base):
    """One row per (analysis, user) like — replaces Supabase `analysis_likes`."""
    __tablename__ = "analysis_likes"
    __table_args__ = (UniqueConstraint("session_id", "user_id", name="uq_like_session_user"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, default=func.now())


class ConversationMessage(Base):
    """Chat timeline — replaces Supabase `conversation_messages`."""
    __tablename__ = "conversation_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    role = Column(String, nullable=False)            # user | assistant | system
    content = Column(Text, nullable=False)
    message_type = Column(String, default="message")  # question | answer | clarification | analysis_result
    message_metadata = Column("metadata", JSON)
    created_at = Column(DateTime, default=func.now(), index=True)


class ConversationStateRecord(Base):
    """Serialised ConversationState shared across Cloud Run instances —
    replaces Supabase `conversations`."""
    __tablename__ = "conversation_states"

    session_id = Column(String, primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    initial_query = Column(Text)
    is_complete = Column(Boolean, default=False)
    state = Column(JSON, nullable=False)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class UserCredits(Base):
    """Credit balance — replaces Supabase `user_credits`."""
    __tablename__ = "user_credits"

    user_id = Column(String, primary_key=True)
    credits = Column(Integer, nullable=False, default=SIGNUP_CREDITS)
    total_used = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class CreditTransaction(Base):
    """Audit log of every credit change — replaces Supabase `credit_transactions`."""
    __tablename__ = "credit_transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, nullable=False, index=True)
    amount = Column(Integer, nullable=False)          # +add / -usage
    transaction_type = Column(String, nullable=False)  # signup_bonus | subscription_renewal | analysis_usage | manual_adjustment
    description = Column(Text)
    analysis_id = Column(String)                       # analyses.session_id
    balance_after = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=func.now(), index=True)


class Subscription(Base):
    """Stripe subscription mirror — replaces Supabase `subscriptions`."""
    __tablename__ = "subscriptions"

    user_id = Column(String, primary_key=True)
    user_email = Column(String)
    stripe_customer_id = Column(String, index=True)
    stripe_subscription_id = Column(String, index=True)
    stripe_price_id = Column(String)
    status = Column(String, nullable=False, default="inactive")  # active | canceled | past_due | trialing | inactive
    plan_type = Column(String, nullable=False, default="free")   # free | pro | enterprise
    current_period_start = Column(DateTime)
    current_period_end = Column(DateTime)
    cancel_at_period_end = Column(Boolean, default=False)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class PaymentHistory(Base):
    """Stripe payments — replaces Supabase `payment_history`."""
    __tablename__ = "payment_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, nullable=False, index=True)
    stripe_payment_intent_id = Column(String)
    stripe_invoice_id = Column(String)
    amount = Column(Integer, nullable=False, default=0)  # cents
    currency = Column(String, default="usd")
    status = Column(String, nullable=False)              # succeeded | pending | failed
    description = Column(Text)
    created_at = Column(DateTime, default=func.now())


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

    # Build database URL.
    #
    # The driver is named explicitly. A bare "postgresql://" resolves to
    # whatever DBAPI the installed SQLAlchemy defaults to, and SQLAlchemy 2.1
    # changed that default from psycopg2 to psycopg (v3). Because
    # requirements.txt is unpinned, the 2026-10-15 build picked up
    # SQLAlchemy 2.1.4, could not import psycopg, and silently fell back to
    # SQLite — so analyses were being written to an ephemeral file in the
    # container instead of Cloud SQL.
    if is_cloud and project_id:
        # Cloud SQL with Unix socket
        cloud_sql_connection = f"{project_id}:us-central1:ndvi3-postgres-instance"
        database_url = f"postgresql+psycopg2://{db_user}:{db_password}@/{db_name}?host=/cloudsql/{cloud_sql_connection}"
        print(f"☁️  Using Cloud SQL: {cloud_sql_connection}")
    else:
        # Local PostgreSQL
        db_host = os.getenv('DB_HOST', 'localhost')
        database_url = f"postgresql+psycopg2://{db_user}:{db_password}@{db_host}:5432/{db_name}"
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


# Columns added to `analyses` after the table was first created. Each entry is
# (column_name, SQL type). Works on both Postgres and SQLite because we check
# the live schema with the SQLAlchemy inspector before issuing ALTER TABLE.
_ANALYSES_EXTRA_COLUMNS = [
    ("thumbnail_url", "VARCHAR"),
    ("map_url", "VARCHAR"),
    ("chart_url", "VARCHAR"),
    ("conversation_history", "JSON"),
    ("user_email", "VARCHAR"),
    ("location_lat", "FLOAT"),
    ("location_lng", "FLOAT"),
    ("parameters", "JSON"),
    ("conversation_id", "VARCHAR"),
    ("processing_time_seconds", "FLOAT"),
    ("is_shared", "BOOLEAN NOT NULL DEFAULT false"),
    ("shared_at", "TIMESTAMP"),
    ("likes", "INTEGER NOT NULL DEFAULT 0"),
]


def _run_migrations(eng):
    """Add columns that may be missing in older databases (idempotent)."""
    from sqlalchemy import inspect as sa_inspect

    try:
        existing = {c["name"] for c in sa_inspect(eng).get_columns("analyses")}
    except Exception as e:
        print(f"⚠️  Migration skipped (cannot inspect analyses): {e}")
        return

    applied = 0
    for name, sql_type in _ANALYSES_EXTRA_COLUMNS:
        if name in existing:
            continue
        # Separate transaction per statement so one failure doesn't poison the rest
        try:
            with eng.begin() as conn:
                conn.execute(text(f"ALTER TABLE analyses ADD COLUMN {name} {sql_type}"))
            applied += 1
        except Exception as e:
            print(f"⚠️  Could not add analyses.{name}: {e}")

    # Helpful indexes for the community / my-analyses queries
    for sql in (
        "CREATE INDEX IF NOT EXISTS ix_analyses_is_shared ON analyses (is_shared)",
        "CREATE INDEX IF NOT EXISTS ix_analyses_conversation_id ON analyses (conversation_id)",
        "CREATE INDEX IF NOT EXISTS ix_analyses_created_at ON analyses (created_at)",
    ):
        try:
            with eng.begin() as conn:
                conn.execute(text(sql))
        except Exception:
            pass

    print(f"✅ Database migrations applied ({applied} column(s) added)")


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
