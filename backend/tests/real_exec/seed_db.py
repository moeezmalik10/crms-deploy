"""Fresh throwaway db with one student and one online, job-capable device."""
import os
from datetime import datetime, timezone

import common
common.configure_env()

if os.path.exists(common.DB_PATH):
    os.remove(common.DB_PATH)

from app import create_app, db
from app.models import Node, User
from app.pool_security import sha256_hex
from werkzeug.security import generate_password_hash

app = create_app()
with app.app_context():
    db.create_all()
    u = User(username="rfexec", email=common.STUDENT_EMAIL,
            password=generate_password_hash(common.STUDENT_PASSWORD), role="student")
    db.session.add(u)
    n = Node(name=common.NODE_NAME, status="online", last_heartbeat=datetime.now(timezone.utc),
            total_cores=4, total_ram_mb=4096, free_storage_gb=50,
            device_key_hash=sha256_hex(common.DEVICE_KEY), sandbox_mode="docker")
    db.session.add(n)
    db.session.commit()

print(f"Seeded 1 student + node {common.NODE_NAME} into {common.DB_PATH}")
