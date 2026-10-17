"""A real-executing stand-in for the node-agent: polls for assigned jobs the same way the real
agent does, downloads the real input, actually runs it as a subprocess (not a stub), and
uploads the real output - unlike backend/tests/load/fake_agents.py, which reports instant
fake success to isolate scheduler fairness. This is for verifying the tree-merge and
checkpoint-hash logic against genuine Random Forest output end to end.
"""
import io
import os
import random
import shutil
import subprocess
import sys
import time
import zipfile

import requests

import common

HEADERS = {"X-Device-Key": common.DEVICE_KEY}


def heartbeat():
    requests.post(f"{common.BASE_URL}/agent/heartbeat", headers=HEADERS, timeout=10, json={
        "host": {"cpu": {"used_percent": random.uniform(5, 20)},
                 "memory": {"free": 2 * 1024**3, "total": 4 * 1024**3},
                 "storage": {"total": 100 * 1024**3, "free": 50 * 1024**3}}})


def run_one(cmd):
    task_id = cmd["task_id"]
    part, parts = cmd.get("part", 1), cmd.get("parts", 1)
    work = os.path.join(common.WORK_DIR, f"job_{task_id}")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(os.path.join(work, "output"), exist_ok=True)

    r = requests.get(f"{common.BASE_URL}{cmd['input_path']}", headers=HEADERS, timeout=60)
    r.raise_for_status()
    fname = r.headers.get("X-File-Name", "input")
    if fname.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            z.extractall(work)
    else:
        with open(os.path.join(work, fname), "wb") as f:
            f.write(r.content)

    requests.post(f"{common.BASE_URL}/agent/tasks/{task_id}/ready", headers=HEADERS, timeout=30,
                 json={"mode": "job", "sandbox": "docker", "pc_name": common.NODE_NAME})

    entry = cmd.get("entry") or "main.py"
    env = dict(os.environ, CRMS_PART=str(part), CRMS_PARTS=str(parts))
    proc = subprocess.run([sys.executable, entry], cwd=work, env=env,
                          capture_output=True, text=True, timeout=120)
    print(f"  part {part}/{parts} (task {task_id}): exit {proc.returncode}\n{proc.stdout}{proc.stderr}")

    buf = io.BytesIO()
    out_dir = os.path.join(work, "output")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(out_dir):
            for fn in files:
                full = os.path.join(root, fn)
                z.write(full, os.path.relpath(full, out_dir))
    requests.post(f"{common.BASE_URL}/agent/jobs/{task_id}/result", headers=HEADERS, timeout=60,
                 data={"exit_code": str(proc.returncode), "output": (proc.stdout + proc.stderr)[-60000:]},
                 files={"result": (f"job_{task_id}_result.zip", buf.getvalue(), "application/zip")})


def main(duration_seconds):
    deadline = time.monotonic() + duration_seconds
    last_heartbeat = 0.0
    print(f"Real-executing agent '{common.NODE_NAME}' running for {duration_seconds}s...")
    while time.monotonic() < deadline:
        if time.monotonic() - last_heartbeat >= 10:
            heartbeat()
            last_heartbeat = time.monotonic()
        try:
            r = requests.get(f"{common.BASE_URL}/agent/tasks/poll/{common.NODE_NAME}",
                             headers=HEADERS, timeout=10)
            data = r.json()
        except requests.RequestException as e:
            print(f"  poll error: {e}")
            data = {}
        if data.get("command") == "start" and data.get("mode") == "job":
            run_one(data)
        else:
            time.sleep(0.5)


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 60)
