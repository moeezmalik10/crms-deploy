"""Windows sandbox for CRMS pool jobs - no Docker needed.

Every job runs:
  * as a separate, hidden, low-privilege Windows account (crms_sb1, crms_sb2, ...), never as the
    PC's owner - so it cannot open the owner's user folder, the agent folder (device key) or
    any folder the owner blocked during setup;
  * inside a Windows Job Object that caps its CPU share, its memory, its number of processes,
    keeps it at below-normal priority, stops it touching the owner's windows and clipboard,
    and kills every process of the job when it ends;
  * in its own work folder that only that account and the owner can use;
  * with no network (Windows Firewall rule for the sandbox accounts) and a disk quota.

The accounts, folder permissions, firewall rule and quota are created once by
setup_sandbox.bat (administrator). This module only needs the file sandbox_users.json that the
setup writes. The account passwords in it are DPAPI-encoded, but what really protects them is the
file's permissions: only the PC owner, SYSTEM and administrators can open it, and the sandbox
accounts are denied the whole agent folder.

Self-test:  venv\\Scripts\\python.exe win_sandbox.py --test     (or test_sandbox.bat)
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

IS_WINDOWS = os.name == "nt"
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "sandbox_users.json")
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

LAST_ERROR = ""          # why the sandbox is not available (shown in the agent window)
_cfg = None
_cfg_mtime = None
_users = {}              # account name -> password
_free = []               # accounts not running a job right now
_busy = set()
_lock = threading.Lock()

# ====================================================================== Win32 definitions
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _crypt = ctypes.WinDLL("crypt32", use_last_error=True)

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                    ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                    ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

    class JOBOBJECT_BASIC_UI_RESTRICTIONS(ctypes.Structure):
        _fields_ = [("UIRestrictionsClass", wintypes.DWORD)]

    class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
        _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                    ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                    ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                    ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                    ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                               wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _k32.QueryInformationJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.TerminateProcess.restype = wintypes.BOOL
    _k32.ResumeThread.argtypes = [wintypes.HANDLE]
    _k32.ResumeThread.restype = wintypes.DWORD
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.LocalFree.argtypes = [ctypes.c_void_p]
    _k32.LocalFree.restype = ctypes.c_void_p
    _adv.CreateProcessWithLogonW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPCWSTR,
        wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)]
    _adv.CreateProcessWithLogonW.restype = wintypes.BOOL
    _crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.POINTER(DATA_BLOB),
                                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(DATA_BLOB)]
    _crypt.CryptUnprotectData.restype = wintypes.BOOL

LOGON_WITH_PROFILE = 0x1
CREATE_SUSPENDED = 0x4
CREATE_NEW_CONSOLE = 0x10
BELOW_NORMAL_PRIORITY_CLASS = 0x4000
STARTF_USESHOWWINDOW = 0x1
SW_HIDE = 0
WAIT_TIMEOUT = 0x102

JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x8
JOB_OBJECT_LIMIT_AFFINITY = 0x10
JOB_OBJECT_LIMIT_PRIORITY_CLASS = 0x20
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4
JOB_OBJECT_UILIMIT_ALL = 0xFF       # handles, clipboard read/write, system settings, display, atoms, desktop, logoff
JobObjectBasicAccountingInformation = 1
JobObjectBasicUIRestrictions = 4
JobObjectExtendedLimitInformation = 9
JobObjectCpuRateControlInformation = 15
MAX_PROCESSES = 64

LAUNCH_HINTS = {
    1326: "the sandbox account password does not match - run setup_sandbox.bat again",
    1327: "the sandbox account is restricted - run setup_sandbox.bat again",
    1331: "the sandbox account is disabled - run setup_sandbox.bat again",
    1385: "Windows policy does not let the sandbox accounts sign in (\"Allow log on locally\")",
    1058: "the Windows service \"Secondary Logon\" is disabled - run setup_sandbox.bat again",
    1068: "the Windows service \"Secondary Logon\" cannot start",
    267: "the job folder could not be opened by the sandbox account",
    5: "access denied while starting the job",
}


class SandboxError(Exception):
    pass


def _win_error(what):
    code = ctypes.get_last_error()
    hint = LAUNCH_HINTS.get(code, "")
    return SandboxError(f"{what} failed: {ctypes.FormatError(code).strip()} (error {code})" + (f" - {hint}" if hint else ""))


# ====================================================================== setup file
def _unprotect(blob):
    """Decrypt a password the setup protected with DPAPI (machine scope + CRMS entropy)."""
    data = ctypes.create_string_buffer(blob, len(blob))
    ent_raw = b"crms-sandbox"
    ent = ctypes.create_string_buffer(ent_raw, len(ent_raw))
    inp = DATA_BLOB(len(blob), ctypes.cast(data, ctypes.POINTER(ctypes.c_char)))
    entb = DATA_BLOB(len(ent_raw), ctypes.cast(ent, ctypes.POINTER(ctypes.c_char)))
    out = DATA_BLOB()
    if not _crypt.CryptUnprotectData(ctypes.byref(inp), None, ctypes.byref(entb), None, None, 0x1, ctypes.byref(out)):
        raise _win_error("decrypting the sandbox password")
    try:
        return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
    finally:
        _k32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def available():
    """True when setup_sandbox.bat was run on this PC and its settings can be used."""
    global _cfg, _cfg_mtime, LAST_ERROR
    if not IS_WINDOWS:
        LAST_ERROR = "Windows only"
        return False
    if not os.path.exists(CONFIG):
        LAST_ERROR = "not set up (run setup_sandbox.bat once as administrator)"
        _cfg = None
        return False
    mtime = os.path.getmtime(CONFIG)
    if _cfg is not None and mtime == _cfg_mtime:
        return True
    try:
        with open(CONFIG, encoding="utf-8-sig") as f:
            cfg = json.load(f)
        users = {}
        entries = cfg.get("users") or []
        if isinstance(entries, dict):        # PowerShell 5.1 writes a one-item list as a plain object
            entries = [entries]
        for u in entries:
            users[u["name"]] = _unprotect(base64.b64decode(u["secret"]))
        if not users:
            raise SandboxError("no sandbox accounts in sandbox_users.json")
        if not os.path.isdir(cfg.get("work_root", "")):
            raise SandboxError(f"work folder {cfg.get('work_root')} is missing")
        if not os.path.exists(cfg.get("python", "")):
            raise SandboxError(f"Python {cfg.get('python')} is missing")
    except Exception as e:
        LAST_ERROR = f"setup file unusable: {e}"
        _cfg = None
        return False
    with _lock:
        _users.clear()
        _users.update(users)
        _free[:] = [n for n in users if n not in _busy]
        _cfg, _cfg_mtime = cfg, mtime
    LAST_ERROR = ""
    return True


def work_root():
    return _cfg["work_root"]


def acquire(timeout=120):
    """Take a free sandbox account for one job: (name, password) or None."""
    end = time.time() + timeout
    while True:
        with _lock:
            if _free:
                name = _free.pop(0)
                _busy.add(name)
                return name, _users[name]
        if time.time() > end:
            return None
        time.sleep(2)


def release(account):
    if not account:
        return
    with _lock:
        _busy.discard(account[0])
        if account[0] in _users and account[0] not in _free:
            _free.append(account[0])


def prepare_dir(path, account_name):
    """Let exactly one sandbox account use this job folder (the work root grants none of them)."""
    r = subprocess.run(["icacls", path, "/grant", f"{account_name}:(OI)(CI)M", "/Q"],
                       capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise SandboxError(f"could not give {account_name} the job folder: {(r.stdout + r.stderr).strip()[:200]}")


# ====================================================================== one sandboxed process tree
class SandboxProcess:
    """cmd.exe running a script as a sandbox account, inside a Job Object with hard limits."""

    def __init__(self, account, work, script, cores, ram_mb):
        name, password = account
        self.job = self.hp = self.ht = None
        self.ram_mb = ram_mb
        self.job = _k32.CreateJobObjectW(None, None)
        if not self.job:
            raise _win_error("CreateJobObject")
        try:
            self._set_limits(cores, ram_mb)
            cmd_exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
            cmdline = ctypes.create_unicode_buffer(f'"{cmd_exe}" /d /s /c ""{script}""')
            si = STARTUPINFOW()
            si.cb = ctypes.sizeof(si)
            si.dwFlags = STARTF_USESHOWWINDOW
            si.wShowWindow = SW_HIDE          # its console window stays hidden
            pi = PROCESS_INFORMATION()
            ok = _adv.CreateProcessWithLogonW(
                name, ".", password, LOGON_WITH_PROFILE, cmd_exe, cmdline,
                CREATE_SUSPENDED | CREATE_NEW_CONSOLE | BELOW_NORMAL_PRIORITY_CLASS,
                None, work, ctypes.byref(si), ctypes.byref(pi))
            if not ok:
                raise _win_error(f"starting the job as {name}")
            self.hp, self.ht = pi.hProcess, pi.hThread
            # Put it in the job BEFORE it runs a single instruction; every child it starts is in the job too
            if not _k32.AssignProcessToJobObject(self.job, self.hp):
                err = _win_error("AssignProcessToJobObject")
                _k32.TerminateProcess(self.hp, 1)
                raise err
            _k32.ResumeThread(self.ht)
        except Exception:
            self.close()
            raise

    def _set_limits(self, cores, ram_mb):
        n = os.cpu_count() or 1
        c = max(1, min(n, int(round(float(cores or 1)))))
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        b = info.BasicLimitInformation
        b.LimitFlags = (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION |
                        JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_JOB_MEMORY |
                        JOB_OBJECT_LIMIT_PRIORITY_CLASS)
        b.ActiveProcessLimit = MAX_PROCESSES
        b.PriorityClass = BELOW_NORMAL_PRIORITY_CLASS
        info.JobMemoryLimit = int(ram_mb) * 1024 * 1024
        if not _k32.SetInformationJobObject(self.job, JobObjectExtendedLimitInformation,
                                            ctypes.byref(info), ctypes.sizeof(info)):
            raise _win_error("setting the memory/process limits")
        if c < n:
            # Hard cap: the whole job never gets more than c of the n cores, spread over all cores
            rate = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
            rate.ControlFlags = JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
            rate.CpuRate = max(1, min(10000, int(c * 10000 / n)))
            if not _k32.SetInformationJobObject(self.job, JobObjectCpuRateControlInformation,
                                                ctypes.byref(rate), ctypes.sizeof(rate)):
                # older Windows: pin the job to c cores instead
                b.LimitFlags |= JOB_OBJECT_LIMIT_AFFINITY
                b.Affinity = (1 << c) - 1
                if not _k32.SetInformationJobObject(self.job, JobObjectExtendedLimitInformation,
                                                    ctypes.byref(info), ctypes.sizeof(info)):
                    raise _win_error("setting the CPU limit")
        ui = JOBOBJECT_BASIC_UI_RESTRICTIONS(JOB_OBJECT_UILIMIT_ALL)
        if not _k32.SetInformationJobObject(self.job, JobObjectBasicUIRestrictions,
                                            ctypes.byref(ui), ctypes.sizeof(ui)):
            raise _win_error("setting the desktop/clipboard restrictions")

    def alive(self):
        return bool(self.hp) and _k32.WaitForSingleObject(self.hp, 0) == WAIT_TIMEOUT

    def kill(self):
        if self.job:
            _k32.TerminateJobObject(self.job, 1)

    def exit_code(self):
        code = wintypes.DWORD()
        if not self.hp or not _k32.GetExitCodeProcess(self.hp, ctypes.byref(code)):
            return 1
        v = code.value
        return v - 2**32 if v >= 2**31 else v

    def peak_mb(self):
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        if _k32.QueryInformationJobObject(self.job, JobObjectExtendedLimitInformation, ctypes.byref(info),
                                          ctypes.sizeof(info), None):
            return info.PeakJobMemoryUsed / 1024 / 1024
        return 0

    def cpu_seconds(self):
        acc = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        if _k32.QueryInformationJobObject(self.job, JobObjectBasicAccountingInformation, ctypes.byref(acc),
                                          ctypes.sizeof(acc), None):
            return (acc.TotalUserTime + acc.TotalKernelTime) / 1e7
        return 0

    def close(self):
        """Kill anything still running in the job and free the handles."""
        if self.job:
            _k32.TerminateJobObject(self.job, 1)
        for h in (self.ht, self.hp, self.job):
            if h:
                _k32.CloseHandle(h)
        self.job = self.hp = self.ht = None


# ====================================================================== pool jobs
def _batch_escape(s):
    s = (s or "").replace("%", "%%")
    return "".join("^" + ch if ch in "^&|<>()" else ch for ch in s)


def build_script(work, runtime, entry, args, part=1, parts=1, python=None, gpp=None):
    """The .cmd file the sandbox account runs: environment, the program, output to a log file."""
    tmp = os.path.join(work, "tmp")
    lines = ["@echo off",
             f'cd /d "{work}"',
             f'set "CRMS_PART={int(part)}"',
             f'set "CRMS_PARTS={int(parts)}"',
             'set "PYTHONDONTWRITEBYTECODE=1"',
             'set "PYTHONIOENCODING=utf-8"',
             f'set "TEMP={tmp}"',
             f'set "TMP={tmp}"']
    a = _batch_escape(args)
    entry = entry.replace("/", "\\")
    if runtime == "python":
        run = f'"{python}" -u "{entry}" {a}'.rstrip()
    else:
        lines.append(f'set "PATH={os.path.dirname(gpp)};%PATH%"')
        run = f'"{gpp}" -O2 -std=c++17 -o app.exe "{entry}" && app.exe {a}'.rstrip()
    lines.append(f'({run}) > "_crms_out.txt" 2>&1')
    lines.append("exit /b %errorlevel%")
    return "\r\n".join(lines) + "\r\n"


def run_job(work, runtime, entry, args, cores, ram_mb, part, parts, account, watch):
    """Run one pool job in the sandbox. watch(alive, kill) enforces time/disk limits and
    cancelling from the website, and returns the reason it stopped the job ("" if it ended by itself)."""
    gpp = _cfg.get("gpp") or ""
    if runtime != "python" and not (gpp and os.path.exists(gpp)):
        return 1, "", "this device has no C++ compiler (install MinGW g++, then run setup_sandbox.bat again)"
    os.makedirs(os.path.join(work, "tmp"), exist_ok=True)
    script = os.path.join(work, "_crms_run.cmd")
    with open(script, "w", encoding="oem" if IS_WINDOWS else "utf-8", errors="replace", newline="") as f:
        f.write(build_script(work, runtime, entry, args, part, parts, _cfg["python"], gpp))
    proc = SandboxProcess(account, work, script, cores, ram_mb)
    try:
        reason = watch(proc.alive, proc.kill)
        while proc.alive():           # the watcher returns after killing; give Windows a moment
            time.sleep(0.2)
        code, peak = proc.exit_code(), proc.peak_mb()
    finally:
        proc.close()
    out = _read_output(os.path.join(work, "_crms_out.txt"))
    if not reason and code != 0 and (peak >= ram_mb * 0.95 or "MemoryError" in out or "bad_alloc" in out):
        reason = f"memory limit of {ram_mb} MB exceeded"
    return code, out, reason


def _read_output(path):
    """Python writes UTF-8 (PYTHONIOENCODING); C++ programs and cmd.exe write the console code page."""
    if not os.path.exists(path):
        return ""
    raw = open(path, "rb").read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("oem" if IS_WINDOWS else "latin-1", errors="replace")


# ====================================================================== self-test
TEST_PROGRAM = r'''
import getpass, json, os, socket, subprocess, sys, time
probe, agent_file, agent_dir = sys.argv[1], sys.argv[2], sys.argv[3]
r = {"user": getpass.getuser()}
def can_read(p):
    try:
        with open(p, "rb") as f:
            f.read(1)
        return True
    except Exception:
        return False
r["read_profile"] = can_read(probe)
r["read_agent"] = can_read(agent_file)
try:
    with open(os.path.join(agent_dir, "sandbox_write_probe.txt"), "w") as f:
        f.write("x")
    r["write_agent"] = True
except Exception:
    r["write_agent"] = False
try:
    socket.create_connection(("1.1.1.1", 443), timeout=5).close()
    r["internet"] = True
except Exception:
    r["internet"] = False
try:
    big = bytearray(400 * 1024 * 1024)
    r["memory_400mb"] = True
    del big
except MemoryError:
    r["memory_400mb"] = False
busy = "import time\nwhile time.process_time() < 2: pass"
t = time.time()
procs = [subprocess.Popen([sys.executable, "-c", busy]) for _ in range(2)]
for p in procs:
    p.wait()
r["cpu_phase_s"] = round(time.time() - t, 2)
os.makedirs("output", exist_ok=True)
with open(os.path.join("output", "selftest.json"), "w") as f:
    json.dump(r, f)
print("self-test program finished")
'''


def selftest():
    print("CRMS Windows sandbox - self-test")
    print("=" * 60)
    if not IS_WINDOWS:
        print("This test only runs on Windows.")
        return 1
    if not available():
        print("FAIL  sandbox not ready:", LAST_ERROR)
        return 1
    me = os.environ.get("USERNAME", "?")
    print(f"Setup found: {len(_users)} accounts, work folder {work_root()}")
    print(f"Python for jobs: {_cfg['python']}")
    results = []

    def check(ok, text, warn=False):
        tag = "PASS" if ok else ("WARN" if warn else "FAIL")
        results.append(tag)
        print(f"{tag}  {text}")

    profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    probe = os.path.join(profile, "crms_sandbox_probe.txt")
    with open(probe, "w") as f:
        f.write("private")
    account = acquire(timeout=5)
    if not account:
        print("FAIL  no free sandbox account (is the agent running jobs right now? stop it and try again)")
        os.remove(probe)
        return 1
    work = os.path.join(work_root(), f"selftest_{int(time.time())}")
    try:
        os.makedirs(os.path.join(work, "output"), exist_ok=True)
        prepare_dir(work, account[0])
        with open(os.path.join(work, "selftest.py"), "w", encoding="utf-8") as f:
            f.write(TEST_PROGRAM)
        args = f'"{probe}" "{CONFIG}" "{HERE}"'
        script = os.path.join(work, "_crms_run.cmd")
        body = build_script(work, "python", "selftest.py", "", 1, 1, _cfg["python"])
        body = body.replace('-u "selftest.py"', f'-u "selftest.py" {args}')
        with open(script, "w", encoding="oem", errors="replace", newline="") as f:
            f.write(body)
        print(f"Starting a test job as {account[0]} (the first start of an account takes ~10 s)...")
        try:
            proc = SandboxProcess(account, work, script, cores=1, ram_mb=256)
        except Exception as e:
            check(False, f"start a job as a sandbox account: {e}")
            return 1
        t0 = time.time()
        while proc.alive() and time.time() - t0 < 120:
            time.sleep(0.5)
        if proc.alive():
            proc.kill()
        code, peak, cpu = proc.exit_code(), proc.peak_mb(), proc.cpu_seconds()
        proc.close()
        out = _read_output(os.path.join(work, "_crms_out.txt"))
        res_file = os.path.join(work, "output", "selftest.json")
        if not os.path.exists(res_file):
            check(False, f"the test job ran but produced no result (exit {code}). Its output:\n{out[-1500:]}")
            return 1
        r = json.load(open(res_file))
        check(r["user"].lower() != me.lower(), f"jobs run as a separate account ({r['user']}), not as you ({me})")
        check(not r["read_profile"], "a job cannot read your files (test file in your user folder)")
        check(not r["read_agent"], "a job cannot read the agent folder (device key and sandbox passwords)")
        check(not r["write_agent"], "a job cannot change the agent folder")
        check(not r["internet"], "a job has no internet access (Windows Firewall rule)")
        check(not r["memory_400mb"], f"memory limit works (400 MB refused under a 256 MB limit; peak {peak:.0f} MB)")
        n = os.cpu_count() or 1
        if n >= 2:
            eff = 4 / max(r["cpu_phase_s"], 0.1)
            check(eff <= 1.4, f"CPU limit works (2 busy processes, 1 core allowed: used about {eff:.1f} cores)")
        else:
            check(True, "CPU limit: this PC has 1 core, nothing to compare", warn=True)
        check(True, "the job's window is hidden and cannot use your clipboard or other windows (job restrictions)")
    finally:
        release(account)
        for p in (probe, os.path.join(HERE, "sandbox_write_probe.txt")):
            try:
                os.remove(p)
            except OSError:
                pass
        rmtree(work)

    # Sign in each remaining account once, so its first real job starts quickly
    others = [n for n in _users if n != account[0]]
    if others:
        print(f"Preparing the other accounts ({', '.join(others)})...")
        for n in others:
            acc = (n, _users[n])
            d = os.path.join(work_root(), f"warm_{n}")
            try:
                os.makedirs(d, exist_ok=True)
                prepare_dir(d, n)
                s = os.path.join(d, "w.cmd")
                with open(s, "w", encoding="oem", newline="") as f:
                    f.write("@echo off\r\nexit /b 0\r\n")
                p = SandboxProcess(acc, d, s, 1, 128)
                t0 = time.time()
                while p.alive() and time.time() - t0 < 60:
                    time.sleep(0.5)
                p.close()
            except Exception as e:
                check(False, f"account {n}: {e}")
            finally:
                rmtree(d)
    print("=" * 60)
    if "FAIL" in results:
        print("Some checks FAILED - jobs are not safe on this PC yet. Fix the FAIL lines, then run test_sandbox.bat.")
        return 1
    print("All checks passed. Restart the agent: it will show  Sandbox : isolated")
    return 0


def rmtree(path):
    """Delete a job folder, including read-only files a job may have left."""
    import shutil
    import stat

    def on_error(func, p, _):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass
    if not os.path.exists(path):
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=on_error)
    else:
        shutil.rmtree(path, onerror=on_error)


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(selftest())
    print(__doc__)
