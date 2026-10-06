"""CRMS resource-pool runtime for the node agent.

Turns this PC into a pool device that lends a slice of its CPU, RAM and disk - never its
desktop. Everything a requester runs is placed in a sandbox:

* Compute jobs  - an isolated Docker container (no network, capped CPU/RAM/processes,
                  read-only system, only its own work folder) that is deleted afterwards.
                  "Light sandbox" (no Docker) is possible only if the owner allows it.
* Workspaces    - a VS Code-in-the-browser container with its own CPU/RAM limit, reached
                  through a private https link and password; deleted when the time is up.
* Storage       - encrypted chunks kept in a folder on this PC; the owner cannot read them.

The agent only makes outgoing HTTPS calls to the CRMS backend, with its device key.
"""
import hashlib
import io
import os
import platform
import re
import secrets
import shutil
import socket
import string
import subprocess
import sys
import threading
import time
import zipfile

import psutil
import requests

AGENT_VERSION = "2.0"
IS_WINDOWS = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

HERE = os.path.dirname(os.path.abspath(__file__))
STORAGE_DIR = os.path.join(HERE, "pool_storage")
WORK_DIR = os.path.join(HERE, "pool_work")
WS_DIR = os.path.join(HERE, "pool_workspaces")

JOB_IMAGES = {"python": "python:3.12-slim", "cpp": "gcc:14"}
WORKSPACE_BASE_IMAGE = "lscr.io/linuxserver/code-server:latest"
WORKSPACE_IMAGE = "crms-workspace:1"
WORKSPACE_DOCKERFILE = f"""FROM {WORKSPACE_BASE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends build-essential python3 python3-pip && rm -rf /var/lib/apt/lists/*
"""

BACKEND = ""
NODE_NAME = socket.gethostname()
API = requests.Session()
KEYED = False
SETTINGS = {"paused": False, "share_storage_gb": None, "allow_light_sandbox": False}
SANDBOX = "none"            # docker / light / none
_lock = threading.Lock()
_workspaces = {}            # task_id -> {"container", "tunnel", "dir", "disk_mb"}
_jobs_cancel = {}           # task_id -> threading.Event


def log(msg):
    print(f"[pool] {msg}")


def _read(name):
    p = os.path.join(HERE, name)
    return open(p, encoding="utf-8").read().strip() if os.path.exists(p) else ""


def _write(name, value):
    with open(os.path.join(HERE, name), "w", encoding="utf-8") as f:
        f.write(value.strip() + "\n")


def _password(n=12):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


# ================================================================== identity / joining
def setup():
    """Load the server address and device key, joining the pool first if needed."""
    global BACKEND, NODE_NAME, KEYED
    for d in (STORAGE_DIR, WORK_DIR, WS_DIR):
        os.makedirs(d, exist_ok=True)
    BACKEND = _read("backend_url.txt").rstrip("/")
    key = _read("device_key.txt")
    if not key:
        code = _read("join_code.txt")
        if not code and sys.stdin and sys.stdin.isatty():
            print("")
            print("=" * 60)
            print(" Join the CRMS resource pool")
            print(" On the website: Contribute Resources -> Add this PC.")
            print(" It shows the server address and a one-time join code.")
            print(" (Press Enter on the join code to run as a lab PC without a key.)")
            print("=" * 60)
            entered = input(f" Server address [{BACKEND or 'https://...onrender.com'}]: ").strip()
            if entered:
                BACKEND = entered.rstrip("/")
                _write("backend_url.txt", BACKEND)
            code = input(" Join code: ").strip()
        if code:
            key = join(code)
    if key:
        API.headers["X-Device-Key"] = key
        KEYED = True
        NODE_NAME = _read("node_name.txt") or NODE_NAME
    API.headers["User-Agent"] = f"crms-agent/{AGENT_VERSION}"
    return BACKEND, NODE_NAME


def join(code):
    global NODE_NAME
    payload = {"code": code.strip().upper(), "hostname": socket.gethostname(),
               "total_cores": psutil.cpu_count(), "total_ram_mb": psutil.virtual_memory().total / 2**20}
    try:
        r = requests.post(f"{BACKEND}/agent/join", json=payload, timeout=90)
        data = r.json()
    except Exception as e:
        log(f"could not join the pool: {e}")
        return ""
    if r.status_code != 200:
        log(f"could not join the pool: {data.get('error')}")
        return ""
    _write("device_key.txt", data["device_key"])
    _write("node_name.txt", data["node_name"])
    jc = os.path.join(HERE, "join_code.txt")
    if os.path.exists(jc):
        os.remove(jc)
    NODE_NAME = data["node_name"]
    log(f"joined the pool as {NODE_NAME}; device key saved in device_key.txt (keep it private)")
    return data["device_key"]


def apply_settings(s):
    if isinstance(s, dict):
        SETTINGS.update(s)
        detect_sandbox()


# ================================================================== hardware report
def _lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def _cpu_ghz():
    try:
        f = psutil.cpu_freq()
        mhz = (f.max or f.current) if f else 0
        return round(mhz / 1000, 2) if mhz else None
    except Exception:
        return None


def _cpu_model():
    name = platform.processor() or ""
    if IS_WINDOWS:
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
            name = winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except Exception:
            pass
    elif os.path.exists("/proc/cpuinfo"):
        for line in open("/proc/cpuinfo"):
            if line.startswith("model name"):
                name = line.split(":", 1)[1].strip()
                break
    return name[:160]


def disk_usage():
    return psutil.disk_usage(os.path.splitdrive(HERE)[0] + os.sep if IS_WINDOWS else HERE)


def hardware_info():
    du = disk_usage()
    return {
        "lan_ip": _lan_ip(),
        "os": f"{platform.system()} {platform.release()}",
        "cpu_model": _cpu_model(),
        "cpu_ghz": _cpu_ghz(),
        "storage_total_gb": round(du.total / 1024**3, 2),
        "storage_free_gb": round(du.free / 1024**3, 2),
        "sandbox_mode": SANDBOX,
        "agent_version": AGENT_VERSION,
    }


# ================================================================== sandbox detection
def docker_ok():
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True,
                           text=True, timeout=20, creationflags=NO_WINDOW)
        return r.returncode == 0 and r.stdout.strip() != ""
    except Exception:
        return False


def detect_sandbox():
    global SANDBOX
    if docker_ok():
        SANDBOX = "docker"
    elif SETTINGS.get("allow_light_sandbox"):
        SANDBOX = "light"
    else:
        SANDBOX = "none"
    return SANDBOX


def _docker(args, timeout=600, **kw):
    return subprocess.run(["docker"] + args, capture_output=True, text=True, timeout=timeout,
                          creationflags=NO_WINDOW, **kw)


def _image_present(image):
    return _docker(["image", "inspect", image], timeout=60).returncode == 0


def prepare_images():
    """Download / build the sandbox images in the background so the first request is fast."""
    if SANDBOX != "docker":
        return
    for img in JOB_IMAGES.values():
        if not _image_present(img):
            log(f"downloading sandbox image {img} (first time only)...")
            r = _docker(["pull", img], timeout=3600)
            log(f"image {img}: {'ready' if r.returncode == 0 else 'FAILED ' + r.stderr.strip()[:200]}")
    ensure_workspace_image()


def ensure_workspace_image():
    if _image_present(WORKSPACE_IMAGE):
        return WORKSPACE_IMAGE
    log("building the workspace image (VS Code + C/C++ + Python, first time only)...")
    r = _docker(["build", "-t", WORKSPACE_IMAGE, "-"], timeout=3600, input=WORKSPACE_DOCKERFILE)
    if r.returncode == 0:
        log("workspace image ready")
        return WORKSPACE_IMAGE
    log(f"workspace image build failed, using the plain image: {r.stderr.strip()[-300:]}")
    if not _image_present(WORKSPACE_BASE_IMAGE):
        _docker(["pull", WORKSPACE_BASE_IMAGE], timeout=3600)
    return WORKSPACE_BASE_IMAGE


def capability_loop():
    last_prepare = 0
    while True:
        detect_sandbox()
        # check/prepare images at start and then once an hour (a failed download is retried then)
        if SANDBOX == "docker" and time.time() - last_prepare > 3600:
            last_prepare = time.time()
            try:
                prepare_images()
            except Exception as e:
                log(f"image preparation error: {e}")
        time.sleep(300)


# ================================================================== helpers
def _post(path, **kw):
    return API.post(f"{BACKEND}{path}", timeout=kw.pop("timeout", 60), **kw)


def _get(path, **kw):
    return API.get(f"{BACKEND}{path}", timeout=kw.pop("timeout", 60), **kw)


def task_status(task_id):
    try:
        r = _get(f"/agent/tasks/{task_id}/status", timeout=20)
        if r.status_code == 404:
            return "deleted"
        return r.json().get("status")
    except Exception:
        return None


def report_error(task_id, reason):
    log(f"task {task_id}: {reason}")
    try:
        _post(f"/agent/tasks/{task_id}/error", json={"reason": reason}, timeout=20)
    except Exception as e:
        log(f"could not report the error: {e}")


def dir_size_mb(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 2**20


def _safe_extract(data, dest):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for m in z.infolist():
            target = os.path.realpath(os.path.join(dest, m.filename))
            if not target.startswith(os.path.realpath(dest) + os.sep) and target != os.path.realpath(dest):
                raise ValueError(f"unsafe path in zip: {m.filename}")
        z.extractall(dest)


def _kill_tree(proc):
    try:
        parent = psutil.Process(proc.pid)
        for c in parent.children(recursive=True):
            c.kill()
        parent.kill()
    except Exception:
        pass


# ================================================================== compute jobs
def run_job(cmd):
    task_id = cmd["task_id"]
    cancel = threading.Event()
    _jobs_cancel[task_id] = cancel
    work = os.path.join(WORK_DIR, f"job_{task_id}")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(os.path.join(work, "output"), exist_ok=True)
    if not IS_WINDOWS:   # the sandbox user (uid 1000) must be able to write its own folder
        os.chmod(work, 0o777); os.chmod(os.path.join(work, "output"), 0o777)
    runtime, entry = cmd.get("runtime", "python"), cmd.get("entry") or "main.py"
    cores, ram_mb = float(cmd.get("cores") or 1), int(cmd.get("ram_mb") or 1024)
    disk_mb, minutes = float(cmd.get("disk_mb") or 500), float(cmd.get("max_minutes") or 10)
    args = cmd.get("args") or ""
    mode = SANDBOX
    try:
        if mode == "none":
            return report_error(task_id, "this device has no sandbox (install Docker Desktop)")
        r = _get(cmd["input_path"], timeout=120)
        if r.status_code != 200:
            return report_error(task_id, f"could not download the job files (HTTP {r.status_code})")
        fname = os.path.basename(r.headers.get("X-File-Name", "input"))
        if fname.lower().endswith(".zip"):
            _safe_extract(r.content, work)
        else:
            with open(os.path.join(work, os.path.basename(entry)), "wb") as f:
                f.write(r.content)
        if not re.fullmatch(r"[\w./ -]+", entry) or ".." in entry:
            return report_error(task_id, "invalid entry file name")
        _post(f"/agent/tasks/{task_id}/ready", json={"mode": "job", "sandbox": mode}, timeout=30)
        log(f"job {task_id}: {runtime} {entry} in a {mode} sandbox ({cores:g} core, {ram_mb} MB, {disk_mb:g} MB disk, {minutes:g} min)")

        if mode == "docker":
            code, out, reason = _run_job_docker(task_id, work, runtime, entry, args, cores, ram_mb, disk_mb, minutes, cancel)
        else:
            code, out, reason = _run_job_light(task_id, work, runtime, entry, args, cores, ram_mb, disk_mb, minutes, cancel)

        if cancel.is_set() and not reason:
            reason = "cancelled"
        # Pack everything the program wrote into output/ plus the run log
        with open(os.path.join(work, "output", "_run_log.txt"), "w", encoding="utf-8", errors="replace") as f:
            f.write(out)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            out_dir = os.path.join(work, "output")
            for root, _, files in os.walk(out_dir):
                for fn in files:
                    full = os.path.join(root, fn)
                    z.write(full, os.path.relpath(full, out_dir))
        resp = _post(f"/agent/jobs/{task_id}/result", timeout=180,
                     data={"exit_code": str(code), "output": out[-60000:], "reason": reason},
                     files={"result": (f"job_{task_id}_result.zip", buf.getvalue(), "application/zip")})
        log(f"job {task_id}: finished (exit {code}{', ' + reason if reason else ''}) -> HTTP {resp.status_code}")
    except Exception as e:
        report_error(task_id, f"job failed on the device: {e}")
    finally:
        _jobs_cancel.pop(task_id, None)
        shutil.rmtree(work, ignore_errors=True)


def _job_command(runtime, entry, args):
    """Shell command run inside the container."""
    if runtime == "python":
        return f'python -u "{entry}" {args}'.strip()
    return f'g++ -O2 -std=c++17 -o /tmp/app "{entry}" && /tmp/app {args}'.strip()


def _watch(task_id, proc_alive, kill, work, disk_mb, minutes, cancel, extra_size=lambda: 0):
    """Enforce time and disk limits; stop when cancelled on the website."""
    start, last_check = time.time(), 0
    while proc_alive():
        time.sleep(1)
        if time.time() - start > minutes * 60:
            kill(); return f"time limit of {minutes:g} min reached"
        if dir_size_mb(work) + extra_size() > disk_mb:
            kill(); return f"disk limit of {disk_mb:g} MB exceeded"
        if time.time() - last_check > 10:
            last_check = time.time()
            st = task_status(task_id)
            if st and st not in ("running", "starting", "allocated"):
                cancel.set(); kill(); return f"stopped on the website ({st})"
    return ""


def _run_job_docker(task_id, work, runtime, entry, args, cores, ram_mb, disk_mb, minutes, cancel):
    name = f"crms-job-{task_id}"
    _docker(["rm", "-f", name], timeout=60)
    shell = _job_command(runtime, entry, args)
    docker_cmd = ["run", "--rm", "--name", name,
                  "--network", "none",                    # no internet, no LAN
                  f"--cpus={cores:g}", f"--memory={ram_mb}m", f"--memory-swap={ram_mb}m",
                  "--pids-limit", "256", "--cap-drop", "ALL",
                  "--security-opt", "no-new-privileges",
                  "--read-only", "--tmpfs", "/tmp:rw,exec,size=256m,mode=1777",
                  "-v", f"{work}:/work", "-w", "/work", "--user", "1000:1000",
                  "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
                  JOB_IMAGES[runtime], "sh", "-c", shell]
    log_path = os.path.join(work, "..", f"job_{task_id}.log")
    with open(log_path, "w", encoding="utf-8", errors="replace") as logf:
        proc = subprocess.Popen(["docker"] + docker_cmd, stdout=logf, stderr=subprocess.STDOUT,
                                creationflags=NO_WINDOW)
        reason = _watch(task_id, lambda: proc.poll() is None,
                        lambda: _docker(["kill", name], timeout=60), work, disk_mb, minutes, cancel)
        proc.wait()
    out = open(log_path, encoding="utf-8", errors="replace").read()
    os.remove(log_path)
    code = proc.returncode
    if code == 137 and not reason:
        reason = f"memory limit of {ram_mb} MB exceeded"
    return code, out, reason


def _run_job_light(task_id, work, runtime, entry, args, cores, ram_mb, disk_mb, minutes, cancel):
    """No Docker: run as a normal low-priority process with CPU, RAM, disk and time limits.
    Only used when the owner allowed it - the program is not isolated from this PC's files."""
    if runtime == "python":
        argv = [sys.executable, "-u", entry] + (args.split() if args else [])
    else:
        gpp = shutil.which("g++")
        if not gpp:
            return 1, "", "this device has no C++ compiler (install Docker Desktop or MinGW g++)"
        exe = os.path.join(work, "app.exe" if IS_WINDOWS else "app")
        c = subprocess.run([gpp, "-O2", "-std=c++17", "-o", exe, entry], cwd=work, capture_output=True,
                           text=True, timeout=300, creationflags=NO_WINDOW)
        if c.returncode != 0:
            return c.returncode, c.stdout + c.stderr, "compile error"
        argv = [exe] + (args.split() if args else [])
    env = {k: v for k, v in os.environ.items() if k.upper() in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "LANG")}
    env["HOME"] = work
    log_path = os.path.join(WORK_DIR, f"job_{task_id}.log")
    with open(log_path, "w", encoding="utf-8", errors="replace") as logf:
        flags = NO_WINDOW | (0x00004000 if IS_WINDOWS else 0)   # BELOW_NORMAL_PRIORITY_CLASS
        proc = subprocess.Popen(argv, cwd=work, stdout=logf, stderr=subprocess.STDOUT, env=env, creationflags=flags)
        try:
            p = psutil.Process(proc.pid)
            p.cpu_affinity(list(range(max(1, int(cores)))))   # only N cores
            if not IS_WINDOWS:
                p.nice(10)
        except Exception:
            pass
        mem_hit = {"v": False}

        def alive():
            if proc.poll() is not None:
                return False
            try:
                rss = psutil.Process(proc.pid).memory_info().rss
                rss += sum(c.memory_info().rss for c in psutil.Process(proc.pid).children(recursive=True))
                if rss > ram_mb * 2**20:
                    mem_hit["v"] = True
                    _kill_tree(proc)
                    return False
            except psutil.Error:
                return False
            return True

        reason = _watch(task_id, alive, lambda: _kill_tree(proc), work, disk_mb, minutes, cancel)
        proc.wait()
    out = open(log_path, encoding="utf-8", errors="replace").read()
    os.remove(log_path)
    if mem_hit["v"] and not reason:
        reason = f"memory limit of {ram_mb} MB exceeded"
    return proc.returncode, out, reason


# ================================================================== workspaces
def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _cloudflared():
    exe = os.path.join(HERE, "cloudflared.exe" if IS_WINDOWS else "cloudflared")
    if os.path.exists(exe):
        return exe
    found = shutil.which("cloudflared")
    if found:
        return found
    asset = "cloudflared-windows-amd64.exe" if IS_WINDOWS else "cloudflared-linux-amd64"
    url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}"
    log("downloading cloudflared (secure tunnel, first time only)...")
    r = requests.get(url, timeout=300)
    r.raise_for_status()
    with open(exe, "wb") as f:
        f.write(r.content)
    if not IS_WINDOWS:
        os.chmod(exe, 0o755)
    return exe


def _open_tunnel(port, timeout=90):
    """Free Cloudflare quick tunnel: a private https address that forwards to this port."""
    proc = subprocess.Popen([_cloudflared(), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=NO_WINDOW)
    found = {"url": None}

    def reader():
        for line in proc.stdout:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and not found["url"]:
                found["url"] = m.group(0)
    threading.Thread(target=reader, daemon=True).start()
    start = time.time()
    while time.time() - start < timeout and not found["url"] and proc.poll() is None:
        time.sleep(0.5)
    if not found["url"]:
        proc.kill()
        raise RuntimeError("could not open the secure tunnel")
    return proc, found["url"]


def start_workspace(task_id, minutes, cores, ram_mb, disk_mb):
    if SANDBOX != "docker":
        return report_error(task_id, "workspaces need Docker Desktop on this device")
    name = f"crms-ws-{task_id}"
    home = os.path.join(WS_DIR, name)
    os.makedirs(home, exist_ok=True)
    pw = _password()
    port = _free_port()
    try:
        image = ensure_workspace_image()
        _docker(["rm", "-f", name], timeout=60)
        r = _docker(["run", "-d", "--name", name,
                     f"--cpus={cores:g}", f"--memory={int(ram_mb)}m", f"--memory-swap={int(ram_mb)}m",
                     "--pids-limit", "512", "--security-opt", "no-new-privileges",
                     "-p", f"127.0.0.1:{port}:8443",          # only reachable through the tunnel
                     "-e", f"PASSWORD={pw}", "-e", "PUID=1000", "-e", "PGID=1000",
                     "-e", "DEFAULT_WORKSPACE=/config/workspace",
                     "-v", f"{home}:/config", image], timeout=600)
        if r.returncode != 0:
            return report_error(task_id, f"could not start the workspace: {r.stderr.strip()[:300]}")
        # wait for the editor to answer locally
        for _ in range(90):
            try:
                if requests.get(f"http://127.0.0.1:{port}/healthz", timeout=2).status_code < 500:
                    break
            except Exception:
                pass
            time.sleep(1)
        tunnel, url = _open_tunnel(port)
    except Exception as e:
        _docker(["rm", "-f", name], timeout=60)
        shutil.rmtree(home, ignore_errors=True)
        return report_error(task_id, f"workspace failed: {e}")
    with _lock:
        _workspaces[task_id] = {"container": name, "tunnel": tunnel, "dir": home, "disk_mb": disk_mb,
                                "ends": time.time() + minutes * 60}
    _post(f"/agent/tasks/{task_id}/ready", timeout=30, json={
        "mode": "remote", "link": url, "username": "workspace", "password": pw, "hostname": NODE_NAME})
    log(f"workspace {task_id}: {url} ({cores:g} core, {ram_mb:g} MB, {disk_mb:g} MB disk, {minutes:g} min)")


def stop_workspace(task_id, reason, notify):
    with _lock:
        ws = _workspaces.pop(task_id, None)
    if not ws:
        return
    try:
        ws["tunnel"].kill()
    except Exception:
        pass
    _docker(["rm", "-f", ws["container"]], timeout=120)
    shutil.rmtree(ws["dir"], ignore_errors=True)
    log(f"workspace {task_id}: removed ({reason})")
    if notify == "stop":
        try:
            _post(f"/agent/tasks/{task_id}/stop", json={"task_id": task_id}, timeout=20)
        except Exception:
            pass
    elif notify == "error":
        report_error(task_id, reason)


def _container_rw_mb(name):
    r = _docker(["ps", "-s", "--filter", f"name=^{name}$", "--format", "{{.Size}}"], timeout=60)
    m = re.match(r"\s*([\d.]+)\s*([kKMG]?B)", r.stdout or "")
    if not m:
        return 0
    mult = {"B": 1 / 2**20, "kB": 1 / 1024, "KB": 1 / 1024, "MB": 1, "GB": 1024}.get(m.group(2), 1)
    return float(m.group(1)) * mult


def workspace_watch_loop():
    while True:
        time.sleep(15)
        with _lock:
            items = list(_workspaces.items())
        for task_id, ws in items:
            if time.time() > ws["ends"]:
                stop_workspace(task_id, "time is up", "stop")
                continue
            st = task_status(task_id)
            if st and st not in ("running", "starting", "allocated"):
                stop_workspace(task_id, f"session {st} on the website", None)
                continue
            used = dir_size_mb(ws["dir"]) + _container_rw_mb(ws["container"])
            if used > ws["disk_mb"]:
                stop_workspace(task_id, f"disk limit of {ws['disk_mb']:g} MB exceeded", "error")


# ================================================================== pooled storage
def _chunk_path(chunk_id):
    return os.path.join(STORAGE_DIR, f"{int(chunk_id)}.chunk")


def storage_loop():
    last_inventory = 0
    while True:
        time.sleep(15)
        if not KEYED:
            continue
        try:
            r = _get("/agent/storage/tasks", timeout=60)
            for t in (r.json().get("tasks", []) if r.status_code == 200 else []):
                try:
                    _storage_task(t)
                except Exception as e:
                    log(f"storage task {t.get('action')} chunk {t.get('chunk_id')} failed: {e}")
            if time.time() - last_inventory > 600:
                last_inventory = time.time()
                held = [int(f[:-6]) for f in os.listdir(STORAGE_DIR) if f.endswith(".chunk") and f[:-6].isdigit()]
                if held:
                    rr = _post("/agent/storage/inventory", json={"chunk_ids": held}, timeout=60)
                    for cid in (rr.json().get("remove", []) if rr.status_code == 200 else []):
                        try:
                            os.remove(_chunk_path(cid))
                        except OSError:
                            pass
        except Exception as e:
            log(f"storage sync error: {e}")


def _storage_task(t):
    action, cid = t["action"], t["chunk_id"]
    if action == "store":
        r = _get(f"/agent/storage/chunks/{cid}", timeout=120)
        if r.status_code != 200:
            return
        if hashlib.sha256(r.content).hexdigest() != t["sha256"]:
            raise ValueError("checksum mismatch")
        if disk_usage().free < len(r.content) + 2 * 1024**3:
            raise ValueError("not enough free disk on this PC")
        tmp = _chunk_path(cid) + ".part"
        with open(tmp, "wb") as f:
            f.write(r.content)
        os.replace(tmp, _chunk_path(cid))
        _post(f"/agent/storage/replicas/{t['replica_id']}/stored", timeout=30)
        log(f"storage: kept encrypted chunk {cid} ({len(r.content) / 2**20:.1f} MB)")
    elif action == "delete":
        try:
            os.remove(_chunk_path(cid))
        except OSError:
            pass
        _post(f"/agent/storage/replicas/{t['replica_id']}/deleted", timeout=30)
        log(f"storage: removed chunk {cid}")
    elif action == "send":
        p = _chunk_path(cid)
        if not os.path.exists(p):
            return
        data = open(p, "rb").read()
        if hashlib.sha256(data).hexdigest() != t["sha256"]:
            raise ValueError("stored chunk is damaged")
        _post(f"/agent/storage/chunks/{cid}/upload", data=data, timeout=120,
              headers={"Content-Type": "application/octet-stream"})
        log(f"storage: sent chunk {cid} back for a download")


def start_background():
    detect_sandbox()
    for target in (capability_loop, workspace_watch_loop, storage_loop):
        threading.Thread(target=target, daemon=True, name=target.__name__).start()
