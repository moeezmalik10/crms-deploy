"""Shared pytest fixtures for the backend test suite.

Runs against a fresh sqlite database per test, not the real Postgres/Supabase instance -
fast and hermetic, but see the note in test_r3_scheduler.py about what sqlite can't prove
(row-level locking via with_for_update).
"""
import os
import sys
import tempfile
import uuid

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_KEY", "")
os.environ.setdefault("CORS_ALLOWED_ORIGINS", "http://localhost:5173")
os.environ.setdefault("IDS_BASE", "")
os.environ["DISABLE_SCHEDULER"] = "true"

import pytest
import sqlite3
from sqlalchemy import event
from sqlalchemy.engine import Engine

# SQLAlchemy does not turn on sqlite's own foreign-key enforcement by default (unlike
# Postgres, where it's always on) - without this, a test against sqlite would not reproduce
# an IntegrityError that the real database raises, giving false confidence that a cascade/FK
# bug is fixed when it was never actually exercised.
@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _):
    if isinstance(dbapi_connection, sqlite3.Connection):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")


@pytest.fixture()
def app():
    # A unique file per test, not a shared/reused one: Windows keeps a stricter lock on a
    # sqlite file than Linux does, so deleting and recreating the same path between tests
    # raced with the previous test's connection pool still closing.
    db_path = os.path.join(tempfile.gettempdir(), f"crms_pytest_{uuid.uuid4().hex}.db")
    db_url = "sqlite:///" + db_path.replace("\\", "/")
    os.environ["DATABASE_URL"] = db_url

    import config as config_module
    # Config.SQLALCHEMY_DATABASE_URI is a class attribute computed once, at whichever test's
    # import first pulls in the `config` module (e.g. anything importing app.allocation_engine)
    # - which may run before this fixture ever sets DATABASE_URL. A later `import config` just
    # returns that already-cached module, so setting the env var again does nothing by itself;
    # set the class attribute directly so every test gets its own fresh sqlite file regardless
    # of import order.
    config_module.Config.SQLALCHEMY_DATABASE_URI = db_url
    # sqlite doesn't understand the Postgres-only connect_args (sslmode, keepalives...)
    config_module.Config.SQLALCHEMY_ENGINE_OPTIONS = {}

    from app import create_app, db as _db, scheduler as _scheduler
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    # app.tasks' functions (process_queue etc.) use scheduler.app_context() directly; the
    # scheduler is a module-level singleton that only initializes once, so without this a
    # test calling one of them directly would run it against a previous test's stale app/db.
    _scheduler.app = flask_app

    yield flask_app

    with flask_app.app_context():
        _db.session.remove()
        _db.engine.dispose()
    try:
        os.remove(db_path)
    except OSError:
        pass  # best-effort cleanup; a lingering handle here doesn't affect test correctness


@pytest.fixture()
def db(app):
    from app import db as _db
    with app.app_context():
        yield _db


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def make_user(db):
    from app.models import User
    from werkzeug.security import generate_password_hash

    def _make(username, email, role="student", password="Password@123"):
        u = User(username=username, email=email, password=generate_password_hash(password), role=role)
        db.session.add(u)
        db.session.commit()
        return u
    return _make


@pytest.fixture()
def make_node(db):
    from app.models import Node
    from datetime import datetime, timezone

    def _make(name, total_cores=8, total_ram_mb=8192, **kw):
        kw.setdefault("status", "online")
        kw.setdefault("last_heartbeat", datetime.now(timezone.utc))
        n = Node(name=name, total_cores=total_cores, total_ram_mb=total_ram_mb, **kw)
        db.session.add(n)
        db.session.commit()
        return n
    return _make


@pytest.fixture()
def token(app):
    def _make(user, role=None):
        from flask_jwt_extended import create_access_token
        with app.app_context():
            return create_access_token(identity=str(user.id), additional_claims={"role": role or user.role})
    return _make


@pytest.fixture()
def auth(token):
    def _headers(user, role=None):
        return {"Authorization": f"Bearer {token(user, role)}"}
    return _headers
