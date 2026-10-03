from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from config import Config
from flask_apscheduler import APScheduler
import os

# Initialize database object
db = SQLAlchemy()
jwt = JWTManager()
scheduler = APScheduler()

def create_app():
    app = Flask(__name__)
    
    # Load configuration from Config class
    app.config.from_object(Config)

    env_origins = os.getenv("CORS_ALLOWED_ORIGINS")
    
    if env_origins:
        # Convert "url1,url2" string into ["url1", "url2"] list
        allowed_origins = [origin.strip() for origin in env_origins.split(",")]
    else:
        # Fallback for local development
        allowed_origins = ["http://localhost:3000", "http://localhost:5173", "http://127.0.0.1:3000"]

    # The dynamic list to CORS
    CORS(app, resources={
        r"/*": {
            "origins": allowed_origins,
            "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            "allow_headers": ["Content-Type", "Authorization"]
        }
    }, supports_credentials=True)
    
    # Initialize extensions
    db.init_app(app)
    jwt.init_app(app)
    
    if not scheduler.running:
        scheduler.init_app(app)
        scheduler.start()

    # Import blueprints/routes
    from app.auth_routes import auth_bp
    from app.admin_routes import admin_bp
    from app.student_routes import student_bp
    from app.agent_routes import agent_bp
    from app.ml_routes import ml_bp

    # REGISTER
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(student_bp)
    app.register_blueprint(agent_bp)
    app.register_blueprint(ml_bp)
    
    #Create DB tables before first request
    with app.app_context():
        from app import tasks
        db.create_all()
        
    return app
