"""Creates a fresh throwaway sqlite db with N_STUDENTS students and N_NODES online, job-capable
devices - deliberately scarce relative to the student count, since that scarcity is what
creates the contention the fairness measurement needs. Run once before start_server.py and
fake_agents.py. Safe to re-run - deletes and recreates the db file first.
"""
import json
import os
import secrets
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

    students = []
    for i in range(common.N_STUDENTS):
        email = f"load{i}@uog.edu.pk"
        u = User(username=f"load{i}", email=email,
                password=generate_password_hash(common.STUDENT_PASSWORD), role="student")
        db.session.add(u)
        students.append({"email": email, "password": common.STUDENT_PASSWORD})

    nodes = []
    now = datetime.now(timezone.utc)
    for i in range(common.N_NODES):
        key = secrets.token_hex(16)
        n = Node(name=f"LOAD-NODE-{i}", status="online", last_heartbeat=now,
                total_cores=4, total_ram_mb=4096, free_storage_gb=50,
                device_key_hash=sha256_hex(key), sandbox_mode="docker")
        db.session.add(n)
        nodes.append({"name": n.name, "device_key": key})

    db.session.commit()

with open(common.STUDENTS_FILE, "w") as f:
    json.dump(students, f)
with open(common.NODES_FILE, "w") as f:
    json.dump(nodes, f)

print(f"Seeded {common.N_STUDENTS} students and {common.N_NODES} nodes into {common.DB_PATH}")
