"""Shared setup for the fairness/load-test harness (Objective 2's success criterion: "No
over-allocation of any PC under load test; Jain's fairness index of 0.9 or higher under
contention"). Everything here points at one throwaway sqlite file, never the real deployment -
see README.md in this directory for how the pieces fit together and how to run them.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "loadtest.db")
DATABASE_URL = "sqlite:///" + DB_PATH.replace("\\", "/")

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5055
BASE_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"

N_STUDENTS = 20
N_NODES = 3            # deliberately scarce - this is what creates the contention to measure
STUDENT_PASSWORD = "LoadTest@123"

STUDENTS_FILE = os.path.join(HERE, "students.json")
NODES_FILE = os.path.join(HERE, "nodes.json")


def configure_env():
    """Must run before importing anything from app.* - Config.SQLALCHEMY_DATABASE_URI and the
    scheduler's startup gate are both read at import/create_app time, same constraint
    tests/conftest.py documents for the pytest harness."""
    os.environ["DATABASE_URL"] = DATABASE_URL
    os.environ.setdefault("JWT_SECRET_KEY", "load-test-secret-key")
    os.environ.setdefault("SUPABASE_URL", "")
    os.environ.setdefault("SUPABASE_KEY", "")
    os.environ.setdefault("CORS_ALLOWED_ORIGINS", f"{BASE_URL}")
    os.environ.setdefault("IDS_BASE", "")   # login proceeds without IDS screening when unset

    backend_dir = os.path.dirname(os.path.dirname(HERE))   # .../backend, two levels above this file
    import sys
    sys.path.insert(0, backend_dir)

    import config as config_module
    config_module.Config.SQLALCHEMY_DATABASE_URI = DATABASE_URL
    config_module.Config.SQLALCHEMY_ENGINE_OPTIONS = {}   # sqlite doesn't know Postgres connect_args
