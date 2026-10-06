import { useEffect, useState } from "react";
import { API_BASE } from "../../src/config";
import { api, ago, Copy, Field, gb, mb, num, State } from "./PoolUI";

// A student adds their own PC to the pool and decides how much of it to lend.
export default function Contribute() {
  const [devices, setDevices] = useState([]);
  const [form, setForm] = useState({ share_cores: 2, share_ram_mb: 4096, share_storage_gb: 10, allow_light_sandbox: false });
  const [join, setJoin] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(Date.now());

  async function load() {
    try { setDevices(await api("/pool/devices?mine=1")); } catch (e) { setErr(e.message); }
  }
  useEffect(() => {
    load();
    const i = setInterval(load, 10000), t = setInterval(() => setNow(Date.now()), 1000);
    return () => { clearInterval(i); clearInterval(t); };
  }, []);

  async function makeCode() {
    setBusy(true); setErr("");
    try { setJoin(await api("/pool/enroll", { method: "POST", body: JSON.stringify(form) })); }
    catch (e) { setErr(e.message); }
    setBusy(false);
  }

  const left = join ? Math.max(0, Math.round((new Date(join.expires_at).getTime() - now) / 1000)) : 0;
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value });

  return (
    <div className="pool">
      <div className="wrap">
        <div>
          <h1>Contribute resources</h1>
          <p className="lead">
            Lend part of your PC to other students. Their work runs in a locked sandbox with the limits you set here.
            They never see your screen or your files, and you can pause or leave at any time.
          </p>
        </div>
        {err && <div className="err">{err}</div>}

        <section>
          <h2>My devices</h2>
          <div className="panel">
            {devices.length === 0 ? (
              <p className="muted" style={{ margin: 0 }}>You have not added a device yet. Use Add this PC below.</p>
            ) : (
              <div className="list">
                {devices.map((d) => <MyDevice key={d.id} d={d} onChange={load} onError={setErr} />)}
              </div>
            )}
          </div>
        </section>

        <section>
          <h2>Add this PC</h2>
          <div className="panel" style={{ display: "flex", flexDirection: "column", gap: 18 }}>
            <div className="grid4">
              <Field label="CPU cores to lend" hint="Sandboxes never get more than this">
                <input type="number" min="0.5" step="0.5" value={form.share_cores} onChange={set("share_cores")} />
              </Field>
              <Field label="Memory to lend (MB)" hint="4096 MB = 4 GB">
                <input type="number" min="256" step="256" value={form.share_ram_mb} onChange={set("share_ram_mb")} />
              </Field>
              <Field label="Storage to lend (GB)" hint="For other students' encrypted files">
                <input type="number" min="0" step="1" value={form.share_storage_gb} onChange={set("share_storage_gb")} />
              </Field>
              <Field label="Jobs without Docker" hint="Only if Docker cannot be installed; less isolated">
                <span className="row" style={{ fontWeight: 400 }}>
                  <input type="checkbox" checked={form.allow_light_sandbox} onChange={set("allow_light_sandbox")} /> Allow
                </span>
              </Field>
            </div>
            <div className="row">
              <button className="btn primary" disabled={busy} onClick={makeCode}>{busy ? "Creating..." : "Create join code"}</button>
              <span className="small muted">The code works once and expires after 30 minutes.</span>
            </div>

            {join && (
              <div className="grid2" style={{ alignItems: "start" }}>
                <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
                  <div>
                    <div className="small muted">Server address</div>
                    <div className="row"><b style={{ wordBreak: "break-all" }}>{API_BASE}</b><Copy value={API_BASE} /></div>
                  </div>
                  <div>
                    <div className="small muted">Join code {left > 0 ? `(valid for ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")})` : "(expired - create a new one)"}</div>
                    <div className="row"><span className="code">{join.code}</span><Copy value={join.code} /></div>
                  </div>
                </div>
                <ol className="steps">
                  <li><a className="btn ghost sm" href="/crms-agent.zip" download>Download the agent</a> and unzip it, for example to <b>C:\CRMS-agent</b>.</li>
                  <li>Install <b>Python 3.12</b> from python.org (tick "Add to PATH").</li>
                  <li>Double-click <b>setup_agent.bat</b> once, then <b>setup_sandbox.bat</b> once (it asks for administrator
                    permission, creates hidden low-privilege accounts that run the jobs, and tests them). Docker Desktop is
                    optional - it adds browser workspaces.</li>
                  <li>Double-click <b>start_agent.bat</b>, then paste the server address and the join code when it asks.</li>
                  <li>Your PC appears under My devices within a minute.</li>
                </ol>
              </div>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}

function MyDevice({ d, onChange, onError }) {
  const [edit, setEdit] = useState(false);
  const [v, setV] = useState({ share_cores: d.share_cores ?? "", share_ram_mb: d.share_ram_mb ?? "", share_storage_gb: d.share_storage_gb ?? "", allow_light_sandbox: d.allow_light_sandbox });

  async function patch(body) {
    try { await api(`/pool/devices/${d.id}`, { method: "PATCH", body: JSON.stringify(body) }); setEdit(false); onChange(); }
    catch (e) { onError(e.message); }
  }
  async function leave() {
    if (!window.confirm(`Take ${d.name} out of the pool? Its device key stops working; you can add it again later with a new join code.`)) return;
    try { await api(`/pool/devices/${d.id}`, { method: "DELETE" }); onChange(); } catch (e) { onError(e.message); }
  }

  return (
    <div className="item">
      <div className="between">
        <div>
          <div className="row"><b style={{ fontSize: "1.05rem" }}>{d.name}</b><State state={d.state} /></div>
          <div className="small muted" style={{ marginTop: 4 }}>
            {d.state === "offline" ? `Last seen ${ago(d.last_seen)}` : `${d.cores} cores${d.cpu_ghz ? ` x ${num(d.cpu_ghz, 2)} GHz` : ""}, ${mb(d.ram_total_mb)} RAM, ${gb(d.storage_free_gb)} disk free`}
            {d.lan_ip ? ` - LAN ${d.lan_ip}` : ""}
          </div>
          <div className="small" style={{ marginTop: 6 }}>
            Lends {d.share_cores ? `${num(d.share_cores)} cores` : "all free cores"}, {d.share_ram_mb ? mb(d.share_ram_mb) : "all free RAM"},{" "}
            {d.share_storage_gb ? `${num(d.share_storage_gb)} GB storage` : "no storage"}
            {d.lent?.tasks ? <span className="chip lent" style={{ marginLeft: 8 }}>in use: {d.lent.jobs} jobs, {d.lent.workspaces} workspaces</span> : null}
            {d.pool_storage_used_mb > 0 && <span className="chip" style={{ marginLeft: 8 }}>{mb(d.pool_storage_used_mb)} of files kept</span>}
          </div>
          <div className="small muted" style={{ marginTop: 4 }}>
            Sandbox: {d.sandbox_mode === "docker" ? "Docker (jobs and workspaces)"
              : d.sandbox_mode === "isolated" ? "Windows sandbox (jobs run as a separate low-privilege account)"
              : d.sandbox_mode === "light" ? "light (no isolation)"
              : "none - run setup_sandbox.bat in the agent folder to run jobs (Docker Desktop also adds workspaces)"}
          </div>
        </div>
        <div className="row">
          {d.state === "paused" && <button className="btn ghost sm" onClick={() => patch({ paused: false })}>Resume sharing</button>}
          {d.state === "online" && <button className="btn ghost sm" onClick={() => patch({ paused: true })}>Pause sharing</button>}
          <button className="btn ghost sm" onClick={() => setEdit(!edit)}>{edit ? "Close" : "Change limits"}</button>
          <button className="btn danger sm" onClick={leave}>Leave pool</button>
        </div>
      </div>
      {edit && (
        <div className="grid4" style={{ marginTop: 14, alignItems: "end" }}>
          <Field label="CPU cores"><input type="number" min="0.5" step="0.5" value={v.share_cores} onChange={(e) => setV({ ...v, share_cores: e.target.value })} /></Field>
          <Field label="Memory (MB)"><input type="number" min="256" step="256" value={v.share_ram_mb} onChange={(e) => setV({ ...v, share_ram_mb: e.target.value })} /></Field>
          <Field label="Storage (GB)"><input type="number" min="0" step="1" value={v.share_storage_gb} onChange={(e) => setV({ ...v, share_storage_gb: e.target.value })} /></Field>
          <div className="row">
            <label className="row small"><input type="checkbox" checked={v.allow_light_sandbox} onChange={(e) => setV({ ...v, allow_light_sandbox: e.target.checked })} /> Jobs without Docker</label>
            <button className="btn primary sm" onClick={() => patch(v)}>Save limits</button>
          </div>
        </div>
      )}
    </div>
  );
}
