import { useState } from "react";
import { API_BASE } from "../../src/config";
import "./pool.css";

// ---------- API ----------
export async function api(path, opts = {}) {
  const isForm = opts.body instanceof FormData;
  const res = await fetch(`${API_BASE}${path}`, {
    ...opts,
    headers: {
      Authorization: `Bearer ${localStorage.getItem("token")}`,
      ...(opts.body && !isForm ? { "Content-Type": "application/json" } : {}),
      ...(opts.headers || {}),
    },
  });
  let data = null;
  try { data = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) {
    if (res.status === 401 || res.status === 422) throw new Error("Your login has expired. Log out and log in again.");
    throw new Error(data?.error || data?.msg || `Server error ${res.status}`);
  }
  return data;
}

export async function download(path, fallbackName) {
  const res = await fetch(`${API_BASE}${path}`, { headers: { Authorization: `Bearer ${localStorage.getItem("token")}` } });
  if (!res.ok) {
    let msg = `Server error ${res.status}`;
    try { msg = (await res.json()).error || msg; } catch { /* ignore */ }
    throw new Error(msg);
  }
  const cd = res.headers.get("Content-Disposition") || "";
  const name = (cd.match(/filename="?([^"]+)"?/) || [])[1] || fallbackName;
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

// ---------- formatting ----------
export const mb = (v) => (v == null ? "-" : v >= 1024 ? `${(v / 1024).toFixed(v >= 10240 ? 0 : 1)} GB` : `${Math.round(v)} MB`);
export const gb = (v) => (v == null ? "-" : v >= 1024 ? `${(v / 1024).toFixed(1)} TB` : `${v >= 100 ? Math.round(v) : Number(v).toFixed(1)} GB`);
export const bytes = (b) => (b == null ? "-" : b >= 1024 ** 3 ? `${(b / 1024 ** 3).toFixed(2)} GB` : b >= 1024 ** 2 ? `${(b / 1024 ** 2).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`);
export const num = (v, d = 1) => (v == null ? "-" : Number(v).toFixed(d).replace(/\.0+$/, ""));

export function ago(iso) {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

// ---------- small pieces ----------
export function State({ state }) {
  const text = { online: "Online", paused: "Paused", offline: "Offline" }[state] || state;
  return <span className={`state ${state}`}><i />{text}</span>;
}

export function Copy({ value, label = "Copy" }) {
  const [done, setDone] = useState(false);
  return (
    <button className="btn ghost sm" type="button" onClick={async () => {
      try { await navigator.clipboard.writeText(value); setDone(true); setTimeout(() => setDone(false), 1500); } catch { /* ignore */ }
    }}>{done ? "Copied" : label}</button>
  );
}

export function Field({ label, hint, children }) {
  return <label className="field">{label}{children}{hint && <span className="hint">{hint}</span>}</label>;
}

// What a device lends to the pool for one resource
export function contribution(d, key) {
  if (key === "cores") {
    const cap = d.share_cores ? Math.min(d.cores, d.share_cores) : d.cores;
    return { total: cap || 0, used: d.lent?.cores || 0 };
  }
  if (key === "ram") {
    const cap = d.share_ram_mb ? Math.min(d.ram_total_mb, d.share_ram_mb) : d.ram_total_mb;
    return { total: cap || 0, used: d.lent?.ram_mb || 0 };
  }
  // storage: what the owner shares for pooled files
  return { total: (d.share_storage_gb || 0) * 1024, used: d.pool_storage_used_mb || 0 };
}

/**
 * The pool drawn as it is built: one segment per device, as wide as what that device
 * contributes. Indigo = lent out now, teal = free to lend, striped = device offline.
 */
export function CapacityStrip({ title, note, devices, kind, fmt }) {
  const order = { online: 0, paused: 1, offline: 2 };
  const rows = devices.map((d) => ({ d, ...contribution(d, kind) })).filter((r) => r.total > 0)
    .sort((a, b) => order[a.d.state] - order[b.d.state]);
  const live = rows.filter((r) => r.d.state === "online");
  const total = live.reduce((a, r) => a + r.total, 0);
  const used = live.reduce((a, r) => a + Math.min(r.used, r.total), 0);
  const all = rows.reduce((a, r) => a + r.total, 0) || 1;
  return (
    <div className="strip">
      <div className="name">{title}{note && <small>{note}</small>}</div>
      <div className="bar" role="img" aria-label={`${title}: ${fmt(used)} lent of ${fmt(total)} available from ${live.length} online devices`}>
        {rows.length === 0 && <div className="seg offline" style={{ flex: 1 }}><span className="tag">No device is sharing this yet</span></div>}
        {rows.map(({ d, total: t, used: u }) => (
          <div key={d.id} className={`seg ${d.state}`} style={{ flexGrow: t / all, flexBasis: 0 }}
               title={`${d.name} (${d.state}) - shares ${fmt(t)}, lent now ${fmt(u)}`}>
            {d.state !== "offline" && <span className="avail" />}
            {d.state !== "offline" && <span className="used" style={{ width: `${Math.min(100, (u / t) * 100)}%` }} />}
            {t / all > 0.12 && <span className="tag">{d.name}</span>}
          </div>
        ))}
      </div>
      <div className="figures"><b>{fmt(total - used)}</b> free<span className="muted"> of {fmt(total)} online</span></div>
    </div>
  );
}

export function Legend() {
  return (
    <div className="legend">
      <span><i style={{ background: "var(--free)" }} />Free to lend</span>
      <span><i style={{ background: "var(--lent)" }} />Lent out now</span>
      <span><i style={{ background: "var(--warn)", opacity: 0.4 }} />Paused by owner</span>
      <span><i style={{ background: "repeating-linear-gradient(135deg,#d9dce1 0 3px,#f3f4f6 3px 6px)" }} />Offline</span>
    </div>
  );
}
