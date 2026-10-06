import { useEffect, useState } from "react";
import { api, ago, CapacityStrip, gb, Legend, mb, num, State } from "./PoolUI";

// The resource pool at a glance. Admins see every device with its IP addresses and can
// pause or remove it; students see the same pool without addresses.
export default function PoolDashboard({ admin = false }) {
  const [devices, setDevices] = useState([]);
  const [t, setT] = useState(null);
  const [err, setErr] = useState("");
  const [updated, setUpdated] = useState(null);
  const [showOffline, setShowOffline] = useState(false);

  async function load() {
    try {
      const [o, d] = await Promise.all([api("/pool/overview"), api(admin ? "/pool/devices" : "/pool/devices?everyone=1")]);
      setT(o); setDevices(d); setErr(""); setUpdated(new Date());
    } catch (e) { setErr(e.message); }
  }
  useEffect(() => { load(); const i = setInterval(load, 10000); return () => clearInterval(i); }, []); // eslint-disable-line

  async function act(d, what) {
    if (what === "remove" && !window.confirm(`Remove ${d.name} from the pool? Its device key stops working at once.`)) return;
    try {
      if (what === "remove") await api(`/pool/devices/${d.id}`, { method: "DELETE" });
      else await api(`/pool/devices/${d.id}`, { method: "PATCH", body: JSON.stringify({ paused: what === "pause" }) });
      load();
    } catch (e) { setErr(e.message); }
  }

  const live = devices.filter((d) => d.state !== "offline");
  const offline = devices.filter((d) => d.state === "offline");
  const shown = showOffline ? [...live, ...offline] : live;

  return (
    <div className="pool">
      <div className="wrap">
        <div className="between">
          <div>
            <h1>Resource pool</h1>
            <p className="lead">
              Every device below lends part of its CPU, memory and disk. Work runs in sandboxes on these devices;
              nobody gets a device's desktop or files.
            </p>
          </div>
          <div className="small muted">{updated ? `Updated ${updated.toLocaleTimeString()}` : "Loading..."}</div>
        </div>

        {err && <div className="err">{err}</div>}

        <section className="panel" style={{ display: "flex", flexDirection: "column", gap: 20 }}>
          <CapacityStrip title="CPU cores" note={t ? `${num(t.ghz_total)} GHz total power online` : ""}
                         devices={devices} kind="cores" fmt={(v) => `${num(v)} cores`} />
          <CapacityStrip title="Memory" note={t ? `${mb(t.ram_free_mb)} free right now` : ""}
                         devices={devices} kind="ram" fmt={mb} />
          <CapacityStrip title="Pooled storage" note={t ? `${t.pool_files} files kept, 2 copies each` : ""}
                         devices={devices} kind="storage" fmt={mb} />
          <Legend />
        </section>

        {t && (
          <section className="facts">
            <div><b>{t.online}</b>online</div>
            <div><b>{t.paused}</b>paused</div>
            <div><b>{t.offline}</b>offline</div>
            <div><b>{t.cores_online}</b>cores in online devices</div>
            <div><b>{mb(t.ram_total_mb)}</b>RAM in online devices</div>
            <div><b>{gb(t.storage_total_gb)}</b>disk in online devices</div>
            <div><b>{num(t.cpu_used_avg)}%</b>average CPU load</div>
            <div><b>{t.jobs_running}</b>jobs and <b>{t.workspaces_running}</b>workspaces running</div>
          </section>
        )}

        <section>
          <div className="between" style={{ alignItems: "baseline" }}>
            <h2>Devices</h2>
            {offline.length > 0 && (
              <button className="btn ghost sm" onClick={() => setShowOffline(!showOffline)}>
                {showOffline ? "Hide" : "Show"} {offline.length} offline
              </button>
            )}
          </div>
          <div className="panel scroll-x" style={{ padding: "6px 10px" }}>
            <table className="devices">
              <thead>
                <tr>
                  <th>Device</th>{admin && <th>IP address</th>}<th>CPU</th><th>Memory and disk</th>
                  <th>Lending</th>
                </tr>
              </thead>
              <tbody>
                {shown.length === 0 && (
                  <tr><td colSpan={admin ? 5 : 4} className="muted">
                    {devices.length ? "No device is online right now." : "No devices yet. Students add theirs from Contribute resources."}
                  </td></tr>
                )}
                {shown.map((d) => <Row key={d.id} d={d} admin={admin} act={act} />)}
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </div>
  );
}

function Row({ d, admin, act }) {
  const ramUsedPct = d.ram_free_mb != null && d.ram_total_mb ? 100 - (d.ram_free_mb / d.ram_total_mb) * 100 : null;
  const diskUsedPct = d.storage_total_gb && d.storage_free_gb != null ? 100 - (d.storage_free_gb / d.storage_total_gb) * 100 : null;
  return (
    <tr className={d.state}>
      <td style={{ minWidth: 150 }}>
        <b>{d.name}</b>
        <div className="small muted">{d.owner ? `by ${d.owner}` : d.kind === "browser" ? "browser device" : "lab PC"}</div>
        <div style={{ marginTop: 6 }}><State state={d.state} /></div>
        {d.state === "offline" && <div className="small muted">last seen {ago(d.last_seen)}</div>}
        {admin && (d.verified || d.state !== "offline") && (
          <div className="row" style={{ marginTop: 8, gap: 6 }}>
            {d.state === "online" && <button className="btn ghost sm" onClick={() => act(d, "pause")}>Pause</button>}
            {d.state === "paused" && <button className="btn ghost sm" onClick={() => act(d, "resume")}>Resume</button>}
            {d.verified && <button className="btn danger sm" onClick={() => act(d, "remove")}>Remove</button>}
          </div>
        )}
      </td>
      {admin && (
        <td className="small nowrap">
          <div>LAN {d.lan_ip || "-"}</div>
          <div>Public {d.public_ip || "-"}</div>
        </td>
      )}
      <td style={{ minWidth: 150 }}>
        <span className="nowrap">{d.cores} cores{d.cpu_ghz ? ` x ${num(d.cpu_ghz, 2)} GHz` : ""}</span>
        {d.cpu_used != null && <div className="meter" title={`${d.cpu_used}% busy`}><i style={{ width: `${d.cpu_used}%` }} /></div>}
        {d.cpu_model && <div className="small muted clip" title={d.cpu_model}>{d.cpu_model}</div>}
      </td>
      <td className="nowrap">
        <div>{d.ram_free_mb != null ? `${mb(d.ram_free_mb)} free` : "-"}<span className="small muted"> of {mb(d.ram_total_mb)} RAM</span></div>
        {ramUsedPct != null && <div className="meter"><i style={{ width: `${ramUsedPct}%` }} /></div>}
        <div style={{ marginTop: 6 }}>{d.storage_free_gb != null ? `${gb(d.storage_free_gb)} free` : "-"}
          {d.storage_total_gb && <span className="small muted"> of {gb(d.storage_total_gb)} disk</span>}</div>
        {diskUsedPct != null && <div className="meter"><i style={{ width: `${diskUsedPct}%` }} /></div>}
      </td>
      <td className="small" style={{ minWidth: 190 }}>
        <div>Shares {d.share_cores ? `${num(d.share_cores)} cores` : "all free cores"}, {d.share_ram_mb ? mb(d.share_ram_mb) : "all free RAM"},{" "}
          {d.share_storage_gb ? `${num(d.share_storage_gb)} GB storage` : "no storage"}</div>
        <div className="row" style={{ marginTop: 6, gap: 6 }}>
          {d.sandbox_mode === "docker" && <span className="chip good">Docker sandbox</span>}
          {d.sandbox_mode === "isolated" && <span className="chip good">Windows sandbox</span>}
          {d.sandbox_mode === "light" && <span className="chip warn">Light sandbox</span>}
          {(!d.sandbox_mode || d.sandbox_mode === "none") && <span className="chip">Sessions only</span>}
          {d.lent?.tasks ? <span className="chip lent">lent {num(d.lent.cores)} cores, {mb(d.lent.ram_mb)}</span> : null}
        </div>
        {(d.lent?.tasks > 0 || d.pool_storage_used_mb > 0) && (
          <div className="muted" style={{ marginTop: 4 }}>
            {d.lent?.tasks ? `${d.lent.jobs} jobs, ${d.lent.workspaces} workspaces` : ""}
            {d.lent?.tasks && d.pool_storage_used_mb > 0 ? "; " : ""}
            {d.pool_storage_used_mb > 0 ? `${mb(d.pool_storage_used_mb)} of files kept` : ""}
          </div>
        )}
      </td>
    </tr>
  );
}
