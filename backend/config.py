from datetime import timezone
import os

class Config:
    # Pull DATABASE_URL from Render env
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL")
    if SQLALCHEMY_DATABASE_URI and SQLALCHEMY_DATABASE_URI.startswith("postgres://"):
        # Fix for SQLAlchemy 1.4+ 
        SQLALCHEMY_DATABASE_URI = SQLALCHEMY_DATABASE_URI.replace("postgres://", "postgresql://", 1)
    
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY")
    if not JWT_SECRET_KEY:
        raise ValueError("JWT_SECRET_KEY environment variable is not set")

    # SUPABASE SPECIFIC CONNECTION STABILITY SETTINGS
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,  # Checks if connection is alive before every query
        "pool_recycle": 280,    # Re-connects before Supabase's 300s timeout
        "pool_size": 10,        # Limits connections to avoid Supabase 'too many connections' error
        "max_overflow": 5,
        "connect_args": {
            "sslmode": "require", # Required for Supabase
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 5,
        }
    }