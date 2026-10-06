from flask import Flask, jsonify
from flask_cors import CORS
import psutil
import socket
import time
import requests
import threading
import pandas as pd
from io import StringIO
from datetime import datetime, timezone
import subprocess
import sys
import os
import logging

app = Flask(__name__)
CORS(app)

# =====================
# LOGGING SETUP
# =====================
def setup_logging():
    log_format  = "%(asctime)s [%(levelname)s] %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(log_format, date_format))
    logger.addHandler(console_handler)
    import builtins
    def logged_print(*args, **kwargs):
        msg = " ".join(str(a) for a in args)
        logging.info(msg)
    builtins.print = logged_print

setup_logging()

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# =====================
# CONFIG
# =====================
# Backend address: written in backend_url.txt next to this file (one line, e.g. https://crms-backend.onrender.com)
_URL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend_url.txt")
BACKEND_BASE_URL   = (open(_URL_FILE, encoding="utf-8").read().strip() if os.path.exists(_URL_FILE) else "http://localhost:8000").rstrip("/")
REGISTER_URL       = f"{BACKEND_BASE_URL}/register_node"
HEARTBEAT_URL      = f"{BACKEND_BASE_URL}/agent/heartbeat"
HEARTBEAT_INTERVAL = 30  # seconds

# ---------- HOST (PHYSICAL MODE) ----------
HOST_USERNAME = "lab_user"
HOST_PASSWORD = "lab_123"

# ---------- RESOURCE POOL ----------
# Joining the pool, device key, sandboxed jobs/workspaces and pooled storage live in
# pool_runtime.py. Requesters get a slice of this PC's resources, never its desktop.
import pool_runtime as pool
_HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_BASE_URL, NODE_NAME = pool.setup()
BACKEND_BASE_URL = BACKEND_BASE_URL or "http://localhost:8000"
pool.BACKEND = BACKEND_BASE_URL
REGISTER_URL = f"{BACKEND_BASE_URL}/register_node"
HEARTBEAT_URL = f"{BACKEND_BASE_URL}/agent/heartbeat"
API = pool.API          # requests session that carries this device's key
# The previous group's Hyper-V VM flow is only used if you create an empty file
# "use_hyperv_vms.txt" next to agent.py (it needs Hyper-V, a prepared VM and admin rights).
USE_HYPERV_VMS = os.path.exists(os.path.join(_HERE, "use_hyperv_vms.txt"))


# ---------- VM CONFIG ----------
# Defines all VMs this machine manages.
# Each VM has its own HTTP port so both can run simultaneously.
VM_CONFIGS = {
    "windows_10": {
        "name":               "windows_10",
        "username":           "student",
        "password":           "123",
        "rdp_port":           3389,
        "agent_server_path":  r"C:\VM_Project\agent_server.py",
        "http_port":          8000,
        "host_dir":           r"C:\VM_Project_Host",
    },
    "windows_10.2": {
        "name":               "windows_10.2",
        "username":           "student",
        "password":           "123",
        "rdp_port":           3389,
        "agent_server_path":  r"C:\VM_Project\agent_server.py",
        "http_port":          8001,
        "host_dir":           r"C:\VM_Project_Host",
    },
}

# ---------- TASK TO VM MAPPING ----------
# Agent reads task_name from backend payload and picks the correct VM.
# Add or change task names here as needed.
TASK_VM_MAP = {
    # VM windows_10 tasks
    "dev_cpp":          "windows_10",
    "ubuntu":           "windows_10",
    "anylogic":         "windows_10",
    "vs_code":          "windows_10",
    "visual_studio":    "windows_10",
    "android_studio":   "windows_10",

    # VM windows_10.2 tasks
    "oracle":           "windows_10.2",
    "sumo":             "windows_10.2",
}

# Default VM if task_name not found in TASK_VM_MAP
DEFAULT_VM = "windows_10"

# =====================
# RUNTIME STATE
# Tracks all active VM sessions simultaneously
# =====================
_state_lock = threading.Lock()
_active_vms = {}  # { "windows_10": {"ip": "x.x.x.x", "proc": <http_proc>} }
_active_ml_tasks = set()  # tracks running ML task_ids to prevent duplicate runs


# =====================
# UTILS
# =====================
def get_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return socket.gethostbyname(socket.gethostname())


# =====================
# METRICS
# =====================
def get_host_metrics():
    cpu_used = psutil.cpu_percent(interval=1)
    memory   = psutil.virtual_memory()
    disk     = pool.disk_usage()
    return {
        "hostname": NODE_NAME,
        "ip":       get_ip(),
        "cpu":      {"used_percent": cpu_used},
        "memory":   {
            "total": memory.total,
            "used":  memory.used,
            "free":  memory.available,
        },
        "storage":  {
            "total": disk.total,
            "used":  disk.used,
            "free":  disk.free,
        },
    }


def build_payload():
    return {
        "timestamp": str(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
        "host":      get_host_metrics(),
        "hardware":  pool.hardware_info(),
    }


# =====================
# REGISTER NODE
# =====================
def register_node():
    try:
        payload = {
            "name":         NODE_NAME,
            "total_cores":  psutil.cpu_count(),
            "total_ram_mb": psutil.virtual_memory().total / (1024 * 1024),
            "hardware":     pool.hardware_info(),
        }
        # Render's free backend sleeps; the first request can take about a minute to wake it.
        r = API.post(REGISTER_URL, json=payload, timeout=90)
        if r.status_code == 200 and "registered" in r.text:
            pool.apply_settings(r.json().get("settings"))
            print(f"[SUCCESS] Node {payload['name']} registered with {BACKEND_BASE_URL}")
            return True
        print(f"[FAILED] Register failed: HTTP {r.status_code} from {REGISTER_URL}: {r.text[:200]}")
        if r.status_code == 401:
            print("         This PC needs a device key: delete device_key.txt and join again from the website.")
        else:
            print("         Is backend_url.txt exactly your crms-backend address from Render?")
    except Exception as e:
        print(f"[FAILED] Register failed: {e}")
    return False


# =====================
# HEARTBEAT LOOP
# =====================
def heartbeat_loop():
    while True:
        try:
            r = API.post(HEARTBEAT_URL, json=build_payload(), timeout=60)
            if r.status_code == 200:
                pool.apply_settings(r.json().get("settings"))
                state = "PAUSED by owner" if pool.SETTINGS.get("paused") else f"sandbox: {pool.SANDBOX}"
                print(f"Heartbeat sent ({datetime.now().strftime('%H:%M:%S')}) - {state}")
            elif r.status_code == 404 and "not registered" in r.text:
                print("Heartbeat refused: node not registered - registering again")
                register_node()
            else:
                print(f"Heartbeat FAILED: HTTP {r.status_code} from {HEARTBEAT_URL} - check backend_url.txt")
        except Exception as e:
            print(f"Heartbeat failed: {e}")
        time.sleep(HEARTBEAT_INTERVAL)


# =====================
# FLASK ENDPOINTS
# =====================

@app.route("/agent_health", methods=["GET"])
def agent_health():
    return jsonify({
        "status":   "running",
        "hostname": NODE_NAME,
        "ip":       get_ip(),
    })


@app.route("/status", methods=["GET"])
def status():
    with _state_lock:
        vms = dict(_active_vms)

    host_ip  = get_ip()
    sessions = {}
    for vm_name, info in vms.items():
        cfg  = VM_CONFIGS.get(vm_name, {})
        port = cfg.get("http_port", 8000)
        sessions[vm_name] = {
            "vm_ip":       info["ip"],
            "link":        f"http://{host_ip}:{port}/index.html",
            "http_server": "running" if info["proc"] and info["proc"].poll() is None else "stopped",
        }

    return jsonify({
        "agent":       "running",
        "hostname":    NODE_NAME,
        "host_ip":     host_ip,
        "sessions":    sessions if sessions else "no active sessions",
        "managed_vms": list(VM_CONFIGS.keys()),
        "task_map":    TASK_VM_MAP,
    })


@app.route("/vm_ip", methods=["GET"])
def vm_ip_endpoint():
    from flask import request as freq
    vm_name = freq.args.get("vm", DEFAULT_VM)
    with _state_lock:
        info = _active_vms.get(vm_name)
    if info:
        return jsonify({"ip": info["ip"]})
    # Fall back to first active VM
    with _state_lock:
        if _active_vms:
            first = list(_active_vms.values())[0]
            return jsonify({"ip": first["ip"]})
    return jsonify({"error": "No active VM session"}), 404


# =====================
# VM HELPERS
# =====================
def resolve_vm_name(task_name):
    """
    Looks up task_name in TASK_VM_MAP to find which VM to start.
    Falls back to DEFAULT_VM if task_name not found.
    """
    if task_name and task_name in TASK_VM_MAP:
        vm_name = TASK_VM_MAP[task_name]
        print(f"[agent.py] Task '{task_name}' mapped to VM '{vm_name}'")
        return vm_name
    print(f"[agent.py] Task '{task_name}' not in TASK_VM_MAP. Using default VM '{DEFAULT_VM}'")
    return DEFAULT_VM


def get_vm_ip(vm_name):
    """
    Waits for VM to get a LAN IP.
    Prefers LAN IP (192.168.x.x) over internal Hyper-V IP (172.x.x.x).
    """
    max_wait = 60
    waited   = 0
    while waited < max_wait:
        output = subprocess.check_output([
            "powershell", "-Command",
            f"(Get-VMNetworkAdapter -VMName '{vm_name}').IPAddresses"
        ]).decode().strip()

        ips = [ip for ip in output.split() if "." in ip and not ip.startswith("169.")]

        for ip in ips:
            if not ip.startswith("172."):
                return ip

        if ips:
            return ips[0]

        time.sleep(3)
        waited += 3

    raise Exception(f"IP not found for VM '{vm_name}' after 60 seconds")


def start_agent_server_on_vm(vm_ip, cfg):
    """Uses WinRM to start agent_server.py on the VM."""
    username   = cfg["username"]
    password   = cfg["password"]
    agent_path = cfg["agent_server_path"]

    print(f"[agent.py] Starting agent_server.py on VM ({vm_ip})...")

    ps = (
        f"$pw  = ConvertTo-SecureString '{password}' -AsPlainText -Force; "
        f"$cred = New-Object PSCredential('{username}', $pw); "
        f"Invoke-Command -ComputerName {vm_ip} -Credential $cred "
        f"-ScriptBlock {{ Start-Process python "
        f"-ArgumentList '{agent_path}' -WindowStyle Hidden }}"
    )

    result = subprocess.run(
        ["powershell", "-Command", ps],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        print(f"[agent.py] WARNING: Could not auto-start agent_server.py on VM.")
        print(f"           Stderr: {result.stderr.strip()}")
    else:
        print("[agent.py] agent_server.py started on VM successfully.")

    time.sleep(4)


def start_http_server(vm_name, cfg):
    """Starts HTTP server for a specific VM on its configured port."""
    port     = cfg["http_port"]
    host_dir = cfg["host_dir"]

    print(f"[agent.py] Starting HTTP server for {vm_name} on port {port}...")

    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port)],
        cwd=host_dir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    time.sleep(1)
    print(f"[agent.py] HTTP server running on port {port}")
    return proc


def stop_http_server(proc, vm_name=""):
    if proc:
        try:
            proc.terminate()
            print(f"[agent.py] HTTP server stopped for {vm_name}.")
        except Exception as e:
            print(f"[agent.py] HTTP server stop error: {e}")


def notify_backend_stop(task_id):
    """Notifies backend that task is complete so it frees the node."""
    try:
        stop_url = f"{BACKEND_BASE_URL}/agent/tasks/{task_id}/stop"
        API.post(stop_url, json={"task_id": task_id}, timeout=10)
        print(f"[agent.py] Backend notified: task {task_id} stopped.")
    except Exception as e:
        print(f"[agent.py] WARNING: Could not notify backend: {e}")


# =====================
# TASK HANDLER
# =====================
# =====================
# ML TASK HANDLER
# =====================
def run_ml_task_subprocess(task_id, chunk_id, model_type, validation_type, dataset_url, start_row=0, end_row=100, fed_round=1, global_weights=None, global_intercept=None):
    # Prevent duplicate runs of the same task
    task_key = f"{task_id}_chunk{chunk_id}"
    if task_key in _active_ml_tasks:
        print(f"[agent.py] Task {task_key} already running — skipping duplicate")
        return
    _active_ml_tasks.add(task_key)
    print(f"[agent.py] Starting ML Runner for task {task_id} (Round {fed_round})")
    try:
        # =========================
        # FIX B: DOWNLOAD ONLY ASSIGNED ROWS (NO FULL FILE IN RAM)
        # Uses pandas skiprows/nrows to avoid loading entire dataset.
        # Supports large files (500MB+) without memory issues.
        # =========================
        print(f"[agent.py] Downloading dataset (rows {start_row}→{end_row} only)...")

        # Row 0 is the header — skiprows skips rows AFTER header
        # nrows limits how many rows to read after skip
        response = requests.get(dataset_url, timeout=60)
        response.raise_for_status()
        csv_stream = StringIO(response.text)
        df = pd.read_csv(
            csv_stream,
            skiprows=range(1, start_row + 1),   # skip rows before our chunk
            nrows=(end_row - start_row)           # only read our assigned rows
        )
        print(f"[agent.py] Loaded {len(df)} rows (chunk {start_row}→{end_row})")
        # =========================
        # PASS DATA TO ML RUNNER
        # =========================
        import tempfile
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        tmp.write(df.to_json())
        tmp.close()
        tmp_path = tmp.name

        # Write federated weights to a separate temp file for ml_runner
        import json as _json
        fed_tmp = tempfile.NamedTemporaryFile(mode="w", suffix="_fed.json", delete=False)
        _json.dump({
            "round":            fed_round,
            "global_weights":   global_weights,
            "global_intercept": global_intercept
        }, fed_tmp)
        fed_tmp.close()
        fed_tmp_path = fed_tmp.name

        ml_runner_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ml_runner.py")
        proc = subprocess.run(
            [
                sys.executable,
                ml_runner_path,
                str(task_id),
                str(model_type),
                str(validation_type),
                str(start_row),
                str(end_row),
                tmp_path,
                fed_tmp_path   # argv[7] — federated params
            ],
            text=True,
            capture_output=True
        )
        os.unlink(fed_tmp_path)
        os.unlink(tmp_path)

        # Log ml_runner stderr (its debug prints)
        if proc.stderr.strip():
            for line in proc.stderr.strip().splitlines():
                print(f"[ml_runner] {line}")

        # Check ml_runner did not crash
        if proc.returncode != 0 or not proc.stdout.strip():
            print(f"[agent.py] ML Runner crashed. Exit code: {proc.returncode}")
            print(f"[agent.py] stderr: {proc.stderr}")
            return

        # =========================
        # PARSE AND SEND RESULT TO BACKEND
        # =========================
        import json
        ml_result = json.loads(proc.stdout.strip())

        # Sanitize weights — convert numpy arrays to plain Python lists
        def to_list(val):
            if val is None:
                return None
            if hasattr(val, "tolist"):
                return val.tolist()
            if isinstance(val, list):
                return [[float(x) for x in row] if isinstance(row, list) else float(row) for row in val]
            return val

        raw_weights   = ml_result.get("weights")
        raw_intercept = ml_result.get("intercept")

        result_payload = {
            "task_id":      str(task_id),
            "chunk_id":     chunk_id,
            "node_id":      NODE_NAME,
            "model_type":   model_type,
            "status":       ml_result.get("status", "completed"),
            "metrics":      ml_result.get("metrics", {}),
            "explanation":  ml_result.get("explanation", {}),
            "weights":      to_list(raw_weights),      # for FedAvg aggregation
            "intercept":    to_list(raw_intercept),    # for FedAvg aggregation
            "round":        fed_round,
        }
        # =========================
        # SAVE RESULT LOCALLY
        # =========================
        BASE_DIR   = r"C:\node-agent"
        OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        file_path = os.path.join(OUTPUT_DIR, f"result_{task_id}_chunk{chunk_id}.json")
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(result_payload, f, indent=4)
        print(f"[agent.py] Result saved → {file_path}")
        # =========================
        # SEND TO BACKEND
        # =========================
        result_url = f"{BACKEND_BASE_URL}/agent/tasks/{task_id}/result"
        res = API.post(result_url, json=result_payload, timeout=30)
        print(f"[agent.py] Result sent to backend → HTTP {res.status_code}")
    except Exception as e:
        import traceback
        print(f"[agent.py] ML Runner failed: {e}")
        print(f"[agent.py] Traceback: {traceback.format_exc()}")
    finally:
        _active_ml_tasks.discard(task_key)


# =====================
# MAIN TASK HANDLER
# =====================
def start_vm_for_task(task_id, duration, mode, task_name=None, chunk_id=1, dataset_url=None, model_type="decision_tree", validation_type="none", start_row=0, end_row=100, fed_round=1, global_weights=None, global_intercept=None, cores=1, ram_mb=2048, disk_mb=3072):

    ready_url = f"{BACKEND_BASE_URL}/agent/tasks/{task_id}/ready"

    # =========================
    # ML TASK DETECTION (ADDED SAFELY)
    # =========================
    ML_TASK_TYPES = {"ml_task", "ml_training", "ml_job_child"}
    is_ml_task = (task_name and task_name.lower() in ML_TASK_TYPES) or (dataset_url and dataset_url != "None")
    if is_ml_task:
        print(f"[agent.py] ML Task detected: {task_id}")
        print(f"[agent.py] Assigned rows: {start_row} → {end_row}")

        # Guard: backend must send a valid dataset_url
        if not dataset_url or dataset_url == "None":
            print(f"[agent.py] ERROR: Backend did not send a valid dataset_url for task {task_id}. Got: {dataset_url}")
            print(f"[agent.py] Tell your backend to include 'dataset_url' in the poll response.")
            return

        try:
            API.post(ready_url, json={"status": "ml_started"}, timeout=10)
            print("[agent.py] Backend notified: ML task started")
        except Exception as e:
            print(f"[agent.py] Failed to notify backend: {e}")

        # =========================
        # FIX A: RUN ML IN DEDICATED THREAD
        # Prevents heartbeat_loop from being starved during heavy CPU training.
        # The polling thread returns immediately; ML runs independently.
        # =========================
        ml_thread = threading.Thread(
            target=run_ml_task_subprocess,
            args=(task_id, chunk_id, model_type, validation_type, dataset_url, start_row, end_row, fed_round, global_weights, global_intercept),
            daemon=True,
            name=f"ml_task_{task_id}"
        )
        ml_thread.start()
        print(f"[agent.py] ML thread started: {ml_thread.name}")

        return  # IMPORTANT: prevent VM execution


    # ======================================================
    # REMOTE MODE
    # Agent picks VM based on task_name from TASK_VM_MAP.
    # ======================================================
    # Remote = a sandboxed workspace that uses a slice of this PC (not its desktop)
    if mode == "remote" and not USE_HYPERV_VMS:
        pool.start_workspace(task_id, duration or 60, float(cores or 1), float(ram_mb or 2048), float(disk_mb or 3072))
        return

    if mode == "remote":

        # Resolve which VM to use based on task_name
        vm_name = resolve_vm_name(task_name)
        cfg     = VM_CONFIGS.get(vm_name)

        if not cfg:
            print(f"[agent.py] ERROR: VM '{vm_name}' not in VM_CONFIGS.")
            return

        print(f"[agent.py] Starting VM '{vm_name}' for task '{task_name}' (task_id: {task_id})")

        # 1. Boot the VM
        subprocess.run([
            "powershell", "-Command",
            f"Start-VM -Name '{vm_name}'"
        ], check=True)

        # 2. Get VM LAN IP
        vm_ip = get_vm_ip(vm_name)
        print(f"[agent.py] VM IP: {vm_ip}")

        # 3. Start HTTP server for this VM
        http_proc = start_http_server(vm_name, cfg)

        # 4. Store in active sessions
        with _state_lock:
            _active_vms[vm_name] = {"ip": vm_ip, "proc": http_proc}

        # 5. Start agent_server.py on VM via WinRM
        start_agent_server_on_vm(vm_ip, cfg)

        # 6. Build browser link
        host_ip     = get_ip()
        browser_url = f"http://{host_ip}:{cfg['http_port']}/index.html"
        print(f"[agent.py] Browser URL: {browser_url}")

        # 7. Send payload to backend
        payload = {
            "mode":      "remote",
            "hostname":  vm_name,
            "ip":        vm_ip,
            "username":  cfg["username"],
            "password":  cfg["password"],
            "port":      cfg["rdp_port"],
            "link":      browser_url,
        }
        API.post(ready_url, json=payload, timeout=10)
        print(f"[agent.py] VM info + link sent to backend: {browser_url}")

        # 8. Auto shutdown after duration
        def shutdown():
            print(f"[agent.py] Session ended for task {task_id}. Cleaning up...")

            with _state_lock:
                info = _active_vms.pop(vm_name, None)

            if info:
                stop_http_server(info["proc"], vm_name)

            # Restore VM to clean snapshot (removes student files, keeps tools)
            print(f"[agent.py] Restoring VM '{vm_name}' to CleanState snapshot...")
            subprocess.run([
                "powershell", "-Command",
                f"Restore-VMSnapshot -VMName '{vm_name}' -Name 'CleanState' -Confirm:$false"
            ])
            print(f"[agent.py] VM '{vm_name}' restored to clean state.")
            
            subprocess.run([
                "powershell", "-Command",
                f"Stop-VM -Name '{vm_name}' -Force"
            ])
            print(f"[agent.py] VM '{vm_name}' stopped.")

            notify_backend_stop(task_id)

        threading.Timer(duration * 60, shutdown).start()
        print(f"[agent.py] VM will stop after {duration} minute(s)")
        

    # ======================================================
    # PHYSICAL MODE
    # ======================================================
    elif mode == "physical":
        print(f"[agent.py] Physical PC reserved for task {task_id}")

        payload = {
            "mode":     "physical",
            "status":   "reserved",
            "hostname": NODE_NAME,
            "ip":       get_ip(),
            "username": HOST_USERNAME,
            "password": HOST_PASSWORD,
        }
        API.post(ready_url, json=payload, timeout=10)
        print("[agent.py] Host PC info sent to backend")

        def shutdown_physical():
            print(f"[agent.py] Physical session ended for task {task_id}.")
            notify_backend_stop(task_id)

        threading.Timer(duration * 60, shutdown_physical).start()
        print(f"[agent.py] Physical PC will be released after {duration} minute(s)")


# =====================
# COMMAND POLLING LOOP
# Polls the backend for new tasks: every 5 seconds on a pool device (so split-job parts start
# quickly), every 15 seconds on a lab PC; straight away again after receiving work.
# Backend sends task_name so agent knows which VM to start.
# =====================
def command_polling_loop():
    idle_wait = 5 if pool.KEYED else 15
    while True:
        got_work = False
        try:
            poll_url = f"{BACKEND_BASE_URL}/agent/tasks/poll/{NODE_NAME}"
            r    = API.get(poll_url, timeout=15)
            data = r.json()
            got_work = data.get("command") == "start"

            if data.get("command") == "start" and data.get("mode") == "job":
                threading.Thread(target=pool.run_job, args=(data,), daemon=True).start()
            elif data.get("command") == "start":
                threading.Thread(
                    target=start_vm_for_task,
                    kwargs={
                        "task_id":         data.get("task_id"),
                        "duration":         data.get("duration", 60),
                        "mode":             data.get("mode", "remote"),
                        "task_name":        data.get("task_type", None),
                        "chunk_id":         data.get("chunk_id", 1),
                        "dataset_url":      data.get("dataset_url", None),
                        "model_type":       data.get("model_type", "decision_tree"),
                        "validation_type":  data.get("validation_type", "none"),
                        "start_row":        data.get("start_row", 0),
                        "end_row":          data.get("end_row", 100),
                        "fed_round":        data.get("round", 1),
                        "global_weights":   data.get("global_weights", None),
                        "global_intercept": data.get("global_intercept", None),
                        "cores":            data.get("cores", 1),
                        "ram_mb":           data.get("ram_mb", 2048),
                        "disk_mb":          data.get("disk_mb", 3072),
                    },
                    daemon=True
                ).start()

        except Exception as e:
            print(f"Polling error: {e}")

        time.sleep(1 if got_work else idle_wait)


# =====================
# MAIN
# =====================
if __name__ == "__main__":
    print("===== AGENT STARTING =====")
    print(f"Host IP      : {get_ip()}")
    print(f"Backend URL  : {BACKEND_BASE_URL}")
    print(f"Managed VMs  : {list(VM_CONFIGS.keys())}")
    print(f"Default VM   : {DEFAULT_VM}")
    print(f"Task Map     : {TASK_VM_MAP}")
    print(f"Heartbeat    : every {HEARTBEAT_INTERVAL} seconds")
    print(f"Node name    : {NODE_NAME}")
    print(f"Device key   : {'yes - verified pool device' if pool.KEYED else 'NO (lab PC mode) - join from the website to contribute'}")
    print(f"Sandbox      : {pool.detect_sandbox()}  (docker = container; isolated = Windows sandbox account + job limits; light = owner allowed, not isolated)")
    if pool.SANDBOX not in ("docker", "isolated") and pool.IS_WINDOWS:
        print(f"Win sandbox  : {pool.win_sandbox.LAST_ERROR}")
    print(f"Pool storage : {pool.STORAGE_DIR}")
    print("=" * 40)

    for attempt in range(1, 4):
        if register_node():
            break
        print(f"         retrying in 15 seconds ({attempt}/3)...")
        time.sleep(15)

    threading.Thread(target=heartbeat_loop,       daemon=True).start()
    threading.Thread(target=command_polling_loop, daemon=True).start()
    pool.start_background()

    print("Heartbeat loop started")
    print("Command polling loop started")
    print("Local status page on 127.0.0.1 only")
    print("=" * 40)

    # The local status page is for this PC only (127.0.0.1). Pool devices open no ports:
    # all pool traffic is outgoing HTTPS to the CRMS backend.
    port = int(os.environ.get("CRMS_AGENT_PORT", "5000"))
    try:
        app.run(host="127.0.0.1", port=port)
    except OSError as e:
        print(f"[FAILED] Port {port} is already in use ({e}).")
        print("         Another agent (for example the local one) is still running - close it and start again.")
