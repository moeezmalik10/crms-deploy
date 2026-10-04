import { useEffect, useRef, useState } from "react";
import { API_BASE } from "../../src/config";

// "Share this device": turns the phone / tablet / laptop that opens this page into a
// CRMS pool node, with nothing to install. It talks to the same backend routes as the
// Python PC agent (register, heartbeat, poll, ready, stop, error).
// Browser limits: free RAM and CPU load cannot be read by a web page, so the owner
// declares how much RAM the device offers, and CPU load is estimated from page timing.

const HEARTBEAT_MS = 20000;   // backend marks a node offline after 45 s without a heartbeat
const POLL_MS = 10000;
const DEV_CPP_NEEDS_MB = 2048 + 768; // backend rule: profile RAM + safety buffer
const RESERVED_MB = 2048;

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

function cleanName(raw) {
  let n = (raw || "").toUpperCase().replace(/[^A-Z0-9-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 36);
  if (!n.startsWith("WEB-")) n = "WEB-" + n;
  return n.replace(/-+$/, "") || "WEB-DEVICE";
}

function defaultName() {
  const saved = store.get("crms_node_name");
  if (saved) return saved;
  const who = (store.get("username") || (/iphone|ipad/i.test(navigator.userAgent) ? "IPHONE" : /android/i.test(navigator.userAgent) ? "PHONE" : "DEVICE"));
  return cleanName(`WEB-${who}-${Math.random().toString(36).slice(2, 6)}`);
}

async function call(method, path, body) {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch { /* empty reply */ }
  return { ok: res.ok, status: res.status, data };
}

export default function ShareDevice() {
  const deviceGb = navigator.deviceMemory;              // Chrome/Android only, rounded, max 8
  const cores = navigator.hardwareConcurrency || 2;
  const standalone = typeof window !== "undefined" && window.location.pathname.startsWith("/node");

  const [name, setName] = useState(defaultName);
  const [offerMb, setOfferMb] = useState(() => Number(store.get("crms_node_offer")) || (deviceGb ? Math.round(deviceGb * 1024 / 2) : 4096));
  const [state, setState] = useState("idle");          // idle | joining | online | error
  const [lastBeat, setLastBeat] = useState(null);
  const [session, setSession] = useState(null);        // { id, endsAt }
  const [now, setNow] = useState(Date.now());
  const [hidden, setHidden] = useState(document.hidden);
  const [log, setLog] = useState([]);

  const timers = useRef([]);
  const wakeLock = useRef(null);
  const cpuEst = useRef(0);
  const sessionRef = useRef(null);
  const handled = useRef(new Set());
  const nodeName = useRef(name);

  const say = (msg) => setLog((l) => [`${new Date().toLocaleTimeString()}  ${msg}`, ...l].slice(0, 10));

  // CPU load estimate: how late a 500 ms timer fires (a busy device fires it late)
  useEffect(() => {
    let last = performance.now();
    const t = setInterval(() => {
      const t2 = performance.now();
      const lag = Math.max(0, t2 - last - 500);
      cpuEst.current = Math.min(100, Math.round(cpuEst.current * 0.7 + (lag / 5) * 0.3));
      last = t2;
    }, 500);
    const tick = setInterval(() => setNow(Date.now()), 1000);
    const vis = () => {
      setHidden(document.hidden);
      if (!document.hidden && timers.current.length) keepAwake();
    };
    document.addEventListener("visibilitychange", vis);
    const bye = () => leave(true);
    window.addEventListener("pagehide", bye);
    return () => { clearInterval(t); clearInterval(tick); document.removeEventListener("visibilitychange", vis); window.removeEventListener("pagehide", bye); leave(true); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function keepAwake() {
    try { if ("wakeLock" in navigator) wakeLock.current = await navigator.wakeLock.request("screen"); } catch { /* not allowed */ }
  }

  function freeMb() {
    return Math.max(0, offerMb - (sessionRef.current ? RESERVED_MB : 0));
  }

  async function register() {
    const r = await call("POST", "/register_node", { name: nodeName.current, total_cores: cores, total_ram_mb: offerMb });
    if (!r.ok) throw new Error(`register: HTTP ${r.status}`);
  }

  async function heartbeat() {
    const free = freeMb() * 1024 * 1024, total = offerMb * 1024 * 1024;
    try {
      const r = await call("POST", "/agent/heartbeat", {
        timestamp: new Date().toISOString().slice(0, 19).replace("T", " "),
        host: {
          hostname: nodeName.current, ip: "browser", device: "browser",
          cpu: { used_percent: cpuEst.current },
          memory: { total, used: total - free, free },
          storage: { total: 0, used: 0, free: 0 },
        },
      });
      if (r.ok) { setLastBeat(new Date()); setState("online"); }
      else if (r.status === 404) { say("Not registered - joining again"); await register(); }
      else { setState("error"); say(`Heartbeat failed: HTTP ${r.status}`); }
    } catch (e) { setState("error"); say(`Heartbeat failed: ${e.message}`); }
  }

  async function poll() {
    try {
      const r = await call("GET", `/agent/tasks/poll/${encodeURIComponent(nodeName.current)}`);
      const cmd = r.data || {};
      if (cmd.command !== "start" || handled.current.has(cmd.task_id)) return;
      handled.current.add(cmd.task_id);
      if (cmd.task_type === "ml_task" || cmd.mode !== "physical") {
        await call("POST", `/agent/tasks/${cmd.task_id}/error`, { reason: "browser node accepts physical sessions only" });
        say(`Task ${cmd.task_id} declined (only physical sessions run here)`);
        return;
      }
      const minutes = Number(cmd.duration) || 15;
      await call("POST", `/agent/tasks/${cmd.task_id}/ready`, {
        mode: "physical", status: "reserved", hostname: nodeName.current, ip: "browser", username: "browser", password: "",
      });
      sessionRef.current = { id: cmd.task_id, endsAt: Date.now() + minutes * 60000 };
      setSession(sessionRef.current);
      say(`Task ${cmd.task_id}: device reserved for ${minutes} min`);
      const t = setTimeout(async () => {
        await call("POST", `/agent/tasks/${cmd.task_id}/stop`, { task_id: cmd.task_id });
        sessionRef.current = null; setSession(null);
        say(`Task ${cmd.task_id}: finished, device released`);
      }, minutes * 60000);
      timers.current.push({ clear: () => clearTimeout(t) });
    } catch (e) { say(`Polling error: ${e.message}`); }
  }

  async function join() {
    const n = cleanName(name);
    setName(n); nodeName.current = n;
    store.set("crms_node_name", n); store.set("crms_node_offer", String(offerMb));
    setState("joining"); say(`Joining the pool as ${n}...`);
    try {
      await register();
      say(`Registered with ${API_BASE.replace(/^https?:\/\//, "")}`);
      await keepAwake();
      await heartbeat();
      await poll();
      const hb = setInterval(heartbeat, HEARTBEAT_MS);
      const pl = setInterval(poll, POLL_MS);
      timers.current.push({ clear: () => clearInterval(hb) }, { clear: () => clearInterval(pl) });
    } catch (e) {
      setState("error");
      say(`Could not join: ${e.message}. The server may be waking up - try again in a minute.`);
    }
  }

  // Tell the backend straight away that this device is gone, so requests it had not
  // started go back to the queue instead of failing a minute later.
  function announceLeave() {
    if (!timers.current.length) return;
    const s = sessionRef.current;
    if (s) {
      try { fetch(`${API_BASE}/agent/tasks/${s.id}/stop`, { method: "POST", keepalive: true, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task_id: s.id }) }); } catch { /* ignore */ }
    }
    const body = JSON.stringify({ name: nodeName.current });
    try {
      if (!(navigator.sendBeacon && navigator.sendBeacon(`${API_BASE}/agent/leave`, new Blob([body], { type: "text/plain" })))) {
        fetch(`${API_BASE}/agent/leave`, { method: "POST", keepalive: true, headers: { "Content-Type": "text/plain" }, body });
      }
    } catch { /* ignore */ }
  }

  function leave(silent) {
    announceLeave();
    timers.current.forEach((t) => t.clear());
    timers.current = [];
    try { wakeLock.current && wakeLock.current.release(); } catch { /* ignore */ }
    wakeLock.current = null;
    if (!silent) { setState("idle"); setSession(null); sessionRef.current = null; say("Left the pool (shows offline within a minute)"); }
  }

  const joined = state === "online" || state === "joining" || (state === "error" && timers.current.length > 0);
  const left = session ? Math.max(0, Math.round((session.endsAt - now) / 1000)) : 0;
  const badge = {
    idle: ["Not in the pool", "bg-gray-300 text-gray-800"],
    joining: ["Joining...", "bg-amber-200 text-amber-900"],
    online: [session ? "Reserved" : "Online - waiting for work", session ? "bg-indigo-200 text-indigo-900" : "bg-green-200 text-green-900"],
    error: ["Connection problem", "bg-red-200 text-red-900"],
  }[state];

  return (
    <div className={`w-full p-4 md:p-8 ${standalone ? "min-h-screen bg-gray-100" : ""}`}>
      <div className="max-w-xl mx-auto flex flex-col gap-5 text-left">
        <div>
          <h1 className="text-[22px] font-bold text-black md:text-3xl">Share this device</h1>
          <p className="text-sm text-[#666] mt-1">
            Adds this phone, tablet or laptop to the CRMS resource pool from the browser. Nothing to install.
            Keep this page open with the screen on while it is in the pool.
          </p>
        </div>

        <div className="bg-white rounded-xl p-5 shadow flex flex-col gap-4">
          <div className="flex items-center justify-between gap-3">
            <span className="font-semibold">Status</span>
            <span className={`px-3 py-1 rounded-full text-sm font-semibold ${badge[1]}`}>{badge[0]}</span>
          </div>

          {session && (
            <div className="rounded-lg bg-indigo-50 border border-indigo-200 p-4">
              <p className="font-semibold text-indigo-900">Reserved for task #{session.id}</p>
              <p className="text-3xl font-bold tabular-nums text-indigo-900 mt-1">
                {String(Math.floor(left / 60)).padStart(2, "0")}:{String(left % 60).padStart(2, "0")}
              </p>
              <p className="text-xs text-indigo-800 mt-1">Released automatically when the time is up.</p>
            </div>
          )}

          <label className="flex flex-col gap-1 text-sm">
            <span className="font-semibold">Device name in the pool</span>
            <input value={name} disabled={joined} onChange={(e) => setName(e.target.value)}
              className="bg-[rgb(216,216,216)] rounded-lg p-3 outline-none border border-gray-400 focus:border-indigo-500 disabled:opacity-60" />
            <span className="text-xs text-[#777]">Always starts with WEB- so the admin can tell browser devices from lab PCs.</span>
          </label>

          <label className="flex flex-col gap-1 text-sm">
            <span className="font-semibold">RAM offered to the pool: {offerMb} MB</span>
            <input type="range" min={1024} max={deviceGb ? deviceGb * 1024 : 16384} step={256} value={offerMb} disabled={joined}
              onChange={(e) => setOfferMb(Number(e.target.value))} />
            <span className={`text-xs ${offerMb >= DEV_CPP_NEEDS_MB ? "text-green-700" : "text-amber-700"}`}>
              {offerMb >= DEV_CPP_NEEDS_MB
                ? "Enough for a Dev C++ physical session."
                : `A Dev C++ session needs at least ${DEV_CPP_NEEDS_MB} MB offered.`}
            </span>
          </label>

          <div className="grid grid-cols-3 gap-2 text-center text-sm">
            <div className="bg-gray-100 rounded-lg p-2"><div className="text-xs text-[#777]">CPU cores</div><div className="font-bold">{cores}</div></div>
            <div className="bg-gray-100 rounded-lg p-2"><div className="text-xs text-[#777]">Device RAM</div><div className="font-bold">{deviceGb ? `${deviceGb} GB` : "unknown"}</div></div>
            <div className="bg-gray-100 rounded-lg p-2"><div className="text-xs text-[#777]">Last heartbeat</div><div className="font-bold">{lastBeat ? lastBeat.toLocaleTimeString() : "-"}</div></div>
          </div>

          {!joined ? (
            <button onClick={join} className="bg-[#1e1e1e] text-white rounded-lg p-3 font-semibold">Join the pool</button>
          ) : (
            <button onClick={() => leave(false)} className="bg-[#f44336] text-white rounded-lg p-3 font-semibold">Leave the pool</button>
          )}

          {joined && hidden && (
            <p className="text-sm text-red-700 font-semibold">This page is in the background. Bring it back to the front, or the device drops out of the pool.</p>
          )}
          {joined && (
            <p className="text-xs text-[#777]">Stay on this page: opening another menu item, logging out or closing the tab takes this device out of the pool.</p>
          )}
        </div>

        <div className="bg-white rounded-xl p-5 shadow">
          <p className="font-semibold mb-2">Activity</p>
          {log.length === 0 ? <p className="text-sm text-[#777]">Nothing yet.</p> : (
            <ul className="text-xs font-mono flex flex-col gap-1 break-words">{log.map((l, i) => <li key={i}>{l}</li>)}</ul>
          )}
        </div>

        <p className="text-xs text-[#777]">
          A browser cannot read live RAM or CPU use, so this node reports the RAM you offer and an estimated CPU load.
          It accepts physical sessions only; ML jobs run on lab PCs with the Python agent.
        </p>
      </div>
    </div>
  );
}
