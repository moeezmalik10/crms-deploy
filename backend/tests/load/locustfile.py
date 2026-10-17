"""Locust scenario: each simulated student logs in once, then repeatedly submits a trivial
pool job against the (deliberately scarce) seeded nodes - the contention this generates is
what fairness gets measured against afterwards, via compute_fairness.py reading the resulting
db. Run with: locust -f locustfile.py --host <BASE_URL> -u <N_STUDENTS> -r <N_STUDENTS> ...
(see README.md for the exact invocation used).
"""
import io
import json
import threading

from locust import HttpUser, between, task

import common

with open(common.STUDENTS_FILE) as f:
    _STUDENTS = json.load(f)
_next_student = iter(range(len(_STUDENTS)))
_lock = threading.Lock()


def _claim_student():
    with _lock:
        i = next(_next_student, None)
    return _STUDENTS[i % len(_STUDENTS)] if i is not None else _STUDENTS[0]


class StudentUser(HttpUser):
    wait_time = between(0.3, 1.0)

    def on_start(self):
        creds = _claim_student()
        r = self.client.post("/auth/login", json={"email": creds["email"], "password": creds["password"]},
                             name="/auth/login")
        self.token = r.json().get("access_token") if r.status_code == 200 else None

    @task
    def submit_job(self):
        if not self.token:
            return
        self.client.post(
            "/pool/jobs",
            headers={"Authorization": f"Bearer {self.token}"},
            files={"file": ("main.py", io.BytesIO(b"print(1)"), "text/plain")},
            data={"runtime": "python", "cores": "1", "ram_mb": "256", "disk_mb": "50", "max_minutes": "1"},
            name="/pool/jobs",
        )
