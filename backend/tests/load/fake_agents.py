"""Simulates the node side only: heartbeats to stay online, polls for assigned jobs, and
reports a trivial success a short, randomised moment later - standing in for a real sandboxed
execution so this harness measures the SCHEDULER's fairness under contention, independent of
real job execution (already covered separately by the sweep/k-fold/RF/compile-test tests).
Never touches a real device key or a real deployment - only ever talks to run_server.py's
local throwaway instance.
"""
import json
import random
import threading
import time

import requests

import common

HEARTBEAT_EVERY = 10
POLL_EVERY = 0.5
WORK_SECONDS = (0.3, 1.2)   # simulated "running" time before reporting completion


def _heartbeat_payload():
    return {"host": {"cpu": {"used_percent": random.uniform(5, 40)},
                     "memory": {"free": 2 * 1024**3, "total": 4 * 1024**3},
                     "storage": {"total": 100 * 1024**3, "free": 50 * 1024**3}}}


def run_node(name, device_key, stop_event):
    headers = {"X-Device-Key": device_key}
    last_heartbeat = 0.0
    completed = 0
    while not stop_event.is_set():
        now = time.monotonic()
        if now - last_heartbeat >= HEARTBEAT_EVERY:
            try:
                requests.post(f"{common.BASE_URL}/agent/heartbeat", headers=headers,
                             json=_heartbeat_payload(), timeout=10)
            except requests.RequestException:
                pass
            last_heartbeat = now
        try:
            r = requests.get(f"{common.BASE_URL}/agent/tasks/poll/{name}", headers=headers, timeout=10)
            data = r.json()
        except requests.RequestException:
            data = {}
        if data.get("command") == "start" and data.get("mode") == "job":
            task_id = data["task_id"]
            time.sleep(random.uniform(*WORK_SECONDS))
            try:
                requests.post(f"{common.BASE_URL}/agent/jobs/{task_id}/result", headers=headers,
                             data={"exit_code": "0", "output": "ok"}, timeout=15)
                completed += 1
            except requests.RequestException:
                pass
        else:
            time.sleep(POLL_EVERY)
    print(f"{name}: completed {completed} jobs")


def main(duration_seconds):
    with open(common.NODES_FILE) as f:
        nodes = json.load(f)
    stop_event = threading.Event()
    threads = [threading.Thread(target=run_node, args=(n["name"], n["device_key"], stop_event), daemon=True)
              for n in nodes]
    for t in threads:
        t.start()
    print(f"{len(threads)} fake agent(s) running for {duration_seconds}s...")
    time.sleep(duration_seconds)
    stop_event.set()
    for t in threads:
        t.join(timeout=5)


if __name__ == "__main__":
    import sys
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 90)
