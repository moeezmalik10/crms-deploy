"""Shared setup for the real-execution verification harness - unlike backend/tests/load/,
which stubs job execution to isolate scheduler fairness, this harness actually RUNS a real
scikit-learn Random Forest training script as a subprocess per part, so the tree-merge and
checkpoint-hash logic get exercised against genuine output, not a hand-built fixture.

Points at its own throwaway sqlite file and port, separate from backend/tests/load/'s, so the
two can run independently without colliding. Never touches the real deployment.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "real_exec.db")
DATABASE_URL = "sqlite:///" + DB_PATH.replace("\\", "/")

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5056
BASE_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"

STUDENT_EMAIL = "rfexec@uog.edu.pk"
STUDENT_PASSWORD = "RfExec@123"
NODE_NAME = "REALEXEC-NODE"
DEVICE_KEY = "realexec-device-key"

WORK_DIR = os.path.join(HERE, "work")   # where the real agent extracts/runs each part


def configure_env():
    os.environ["DATABASE_URL"] = DATABASE_URL
    os.environ.setdefault("JWT_SECRET_KEY", "real-exec-secret-key")
    os.environ.setdefault("SUPABASE_URL", "")
    os.environ.setdefault("SUPABASE_KEY", "")
    os.environ.setdefault("CORS_ALLOWED_ORIGINS", BASE_URL)
    os.environ.setdefault("IDS_BASE", "")

    backend_dir = os.path.dirname(os.path.dirname(HERE))   # .../backend
    import sys
    sys.path.insert(0, backend_dir)

    import config as config_module
    config_module.Config.SQLALCHEMY_DATABASE_URI = DATABASE_URL
    config_module.Config.SQLALCHEMY_ENGINE_OPTIONS = {}
