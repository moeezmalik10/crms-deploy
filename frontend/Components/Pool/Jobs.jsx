import { useEffect, useRef, useState } from "react";
import { api, ago, download, Field, mb } from "./PoolUI";

const STATUS = {
  pending: ["Waiting for a device", "chip warn"],
  allocated: ["Sent to a device", "chip lent"],
  starting: ["Starting", "chip lent"],
  running: ["Running", "chip lent"],
  completed: ["Finished", "chip good"],
  failed: ["Failed", "chip"],
};

// Run code on the pool: upload it, choose the slice of CPU/RAM/disk it may use,
// and download what it produced. The code runs in a sandbox on a contributor's PC.
export default function Jobs() {
  const [jobs, setJobs] = useState([]);
  const [form, setForm] = useState({ runtime: "python", entry: "", args: "", cores: 1, ram_mb: 1024, disk_mb: 500, max_minutes: 10 });
  const [file, setFile] = useState(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(null);
  const fileRef = useRef();

  async function load() {
    try { setJobs(await api("/pool/jobs")); } catch (e) { setErr(e.message); }
  }
  useEffect(() => { load(); const i = setInterval(load, 4000); return () => clearInterval(i); }, []);

  async function submit(e) {
    e.preventDefault();
    if (!file) return setErr("Choose a .py, .cpp or .zip file first.");
    setBusy(true); setErr("");
    const fd = new FormData();
    fd.append("file", file);
    Object.entries(form).forEach(([k, v]) => fd.append(k, v));
    try {
      const r = await api("/pool/jobs", { method: "POST", body: fd });
      setOpen(r.job.id); setFile(null); if (fileRef.current) fileRef.current.value = ""; load();
    } catch (e2) { setErr(e2.message); }
    setBusy(false);
  }

  async function remove(j) {
    const active = ["pending", "allocated", "starting", "running"].includes(j.status);
    if (active && !window.confirm("Cancel this job? The sandbox is stopped on the device.")) return;
    try { await api(`/pool/jobs/${j.id}`, { method: "DELETE" }); load(); } catch (e) { setErr(e.message); }
  }

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  const isZip = file && file.name.toLowerCase().endsWith(".zip");

  return (
    <div className="pool">
      <div className="wrap">
        <div>
          <h1>Run a job</h1>
          <p className="lead">
            Upload a program and the pool runs it on a free contributor PC, inside a sandbox with no internet access and
            exactly the cores, memory and disk you ask for. Save files to the <b>output</b> folder to get them back.
          </p>
        </div>
        {err && <div className="err">{err}</div>}

        <form className="panel" onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <div className="grid4">
            <Field label="Language">
              <select value={form.runtime} onChange={set("runtime")}>
                <option value="python">Python 3</option>
                <option value="cpp">C++ (g++)</option>
              </select>
            </Field>
            <Field label="Program" hint=".py, .cpp, or a .zip with several files (max 10 MB)">
              <input ref={fileRef} type="file" accept=".py,.cpp,.cc,.zip" onChange={(e) => setFile(e.target.files[0] || null)} />
            </Field>
            <Field label="File to run" hint={isZip ? "The main file inside the zip" : "Only needed for a zip"}>
              <input type="text" placeholder={form.runtime === "python" ? "main.py" : "main.cpp"} value={form.entry} onChange={set("entry")} disabled={!isZip} />
            </Field>
            <Field label="Arguments" hint="Passed to your program">
              <input type="text" placeholder="e.g. 1000 fast" value={form.args} onChange={set("args")} />
            </Field>
            <Field label="CPU cores"><input type="number" min="1" max="16" value={form.cores} onChange={set("cores")} /></Field>
            <Field label="Memory (MB)"><input type="number" min="128" step="128" value={form.ram_mb} onChange={set("ram_mb")} /></Field>
            <Field label="Disk (MB)"><input type="number" min="50" step="50" value={form.disk_mb} onChange={set("disk_mb")} /></Field>
            <Field label="Time limit (minutes)"><input type="number" min="1" max="240" value={form.max_minutes} onChange={set("max_minutes")} /></Field>
          </div>
          <div className="row">
            <button className="btn primary" disabled={busy}>{busy ? "Uploading..." : "Run on the pool"}</button>
            <span className="small muted">You can have 3 jobs waiting or running at once.</span>
          </div>
        </form>

        <section>
          <h2>My jobs</h2>
          <div className="panel">
            {jobs.length === 0 ? <p className="muted" style={{ margin: 0 }}>No jobs yet.</p> : (
              <div className="list">
                {jobs.map((j) => {
                  const [label, cls] = STATUS[j.status] || [j.status, "chip"];
                  const active = ["pending", "allocated", "starting", "running"].includes(j.status);
                  return (
                    <div className="item" key={j.id}>
                      <div className="between">
                        <div>
                          <div className="row">
                            <b>#{j.id} {j.entry}</b><span className={cls}>{label}</span>
                            {j.exit_code != null && <span className="small muted">exit code {j.exit_code}</span>}
                          </div>
                          <div className="small muted" style={{ marginTop: 4 }}>
                            {j.runtime === "cpp" ? "C++" : "Python"} - {j.cores} {j.cores === 1 ? "core" : "cores"}, {mb(j.ram_mb)} RAM, {mb(j.disk_mb)} disk, {j.max_minutes} min
                            {j.device ? ` - on ${j.device}` : ""} - submitted {ago(j.created_at)}
                          </div>
                          {j.reason && (j.status === "pending" || j.status === "failed") && (
                            <div className="small" style={{ marginTop: 4, color: j.status === "failed" ? "var(--bad)" : "var(--warn)" }}>{j.reason}</div>
                          )}
                        </div>
                        <div className="row">
                          {j.output && <button className="btn ghost sm" onClick={() => setOpen(open === j.id ? null : j.id)}>{open === j.id ? "Hide output" : "Show output"}</button>}
                          {j.has_result && <button className="btn ghost sm" onClick={() => download(`/pool/jobs/${j.id}/result`, `job_${j.id}_result.zip`).catch((e) => setErr(e.message))}>Download results</button>}
                          <button className="btn danger sm" onClick={() => remove(j)}>{active ? "Cancel" : "Delete"}</button>
                        </div>
                      </div>
                      {open === j.id && j.output && <pre className="out" style={{ marginTop: 10 }}>{j.output}</pre>}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
