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
  const [parts, setParts] = useState(1);
  const [kind, setKind] = useState("");          // "" = custom job, "sweep" = parameter sweep
  const [paramSets, setParamSets] = useState("");
  const [data, setData] = useState(null);
  const [header, setHeader] = useState(true);
  const [ready, setReady] = useState(null);   // devices that can run a job right now
  const dataRef = useRef();
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(null);
  const fileRef = useRef();

  async function load() {
    try { setJobs(await api("/pool/jobs")); } catch (e) { setErr(e.message); }
    try {
      const d = await api("/pool/devices?everyone=1");
      setReady(d.filter((x) => x.state === "online" && ["docker", "isolated", "light"].includes(x.sandbox_mode)).length);
    } catch { /* the list still works without this */ }
  }
  useEffect(() => { load(); const i = setInterval(load, 4000); return () => clearInterval(i); }, []);

  async function submit(e) {
    e.preventDefault();
    if (!file) return setErr("Choose a .py, .cpp or .zip file first.");
    if (kind === "sweep" && paramSets.trim().split("\n").filter((s) => s.trim()).length < 2) {
      return setErr("A parameter sweep needs at least 2 parameter sets, one per line.");
    }
    setBusy(true); setErr("");
    const fd = new FormData();
    fd.append("file", file);
    Object.entries(form).forEach(([k, v]) => fd.append(k, v));
    if (kind === "sweep") {
      fd.append("kind", "sweep");
      fd.append("param_sets", paramSets);
    } else {
      fd.append("parts", parts);
    }
    if (data) { fd.append("data", data); fd.append("header", header ? "1" : "0"); }
    try {
      const r = await api("/pool/jobs", { method: "POST", body: fd });
      setOpen(r.job.id); setFile(null); setData(null);
      if (fileRef.current) fileRef.current.value = "";
      if (dataRef.current) dataRef.current.value = "";
      load();
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
          <Field label="Job type" hint="A sweep runs a different parameter set per device and keeps the best score">
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="">Custom - run my own code</option>
              <option value="sweep">Parameter sweep - try several parameter sets at once</option>
            </select>
          </Field>
          {kind === "sweep" ? (
            <Field label="Parameter sets (one per line, one device per line)"
                  hint="Each line becomes that part's Arguments. Your program must write output/result.json with a numeric score field.">
              <textarea rows={4} placeholder={"--lr 0.01 --depth 3\n--lr 0.1 --depth 5\n--lr 0.05 --depth 4"}
                        value={paramSets} onChange={(e) => setParamSets(e.target.value)} />
            </Field>
          ) : (
            <div className="split">
              <Field label="Split across devices" hint={ready == null ? "" : `${ready} ${ready === 1 ? "device" : "devices"} can run jobs right now`}>
                <select value={parts} onChange={(e) => setParts(Number(e.target.value))}>
                  <option value={1}>No - run on one device</option>
                  {[2, 3, 4, 5, 6, 7, 8].map((n) => <option key={n} value={n}>{n} parts, on {n} devices</option>)}
                </select>
              </Field>
              <Field label="Data file (optional)" hint={parts > 1 ? "Rows are shared out: each part gets its own slice" : "Copied next to your program"}>
                <input ref={dataRef} type="file" accept=".csv,.txt,.tsv,.jsonl" onChange={(e) => setData(e.target.files[0] || null)} />
              </Field>
              <label className="check">
                <input type="checkbox" checked={header} onChange={(e) => setHeader(e.target.checked)} disabled={!data} />
                First row is a header (kept in every part)
              </label>
            </div>
          )}
          {kind !== "sweep" && parts > 1 && (
            <p className="small muted" style={{ margin: 0, maxWidth: "80ch" }}>
              Each part runs at the same time on a different device, with the cores and memory above <b>per part</b>.
              Your program reads <code>CRMS_PART</code> and <code>CRMS_PARTS</code> (e.g. part 2 of {parts}) or simply
              processes the data slice it was given. CSV files the parts write to <b>output</b> are joined back into one.
              Splitting pays off when the job takes more than about a minute; each part needs a few seconds to start.
              {ready != null && parts > ready && <span style={{ color: "var(--warn)" }}> Only {ready} can run now, so some parts will wait or share a device.</span>}
            </p>
          )}
          {kind === "sweep" && (
            <p className="small muted" style={{ margin: 0, maxWidth: "80ch" }}>
              Each line runs at the same time on a different device with the arguments from that line instead of the
              Arguments field above. When every part finishes, the job's result is the part with the highest score
              from its <code>output/result.json</code> (<code>{"{\"score\": 0.93}"}</code>).
            </p>
          )}
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
                            {j.kind === "sweep" && <span className="chip">parameter sweep</span>}
                            {j.group && <span className="chip">{j.parts_done}/{j.parts.length} parts finished</span>}
                            {j.best_part != null && <span className="chip good">best: part {j.best_part}, score {j.best_score}</span>}
                            {j.exit_code != null && <span className="small muted">exit code {j.exit_code}</span>}
                          </div>
                          <div className="small muted" style={{ marginTop: 4 }}>
                            {j.runtime === "cpp" ? "C++" : "Python"} - {j.cores} {j.cores === 1 ? "core" : "cores"}, {mb(j.ram_mb)} RAM, {mb(j.disk_mb)} disk, {j.max_minutes} min
                            {j.group ? " per part" : ""}
                            {j.device ? ` - on ${j.device}` : ""}{j.group && j.devices.length ? ` - on ${j.devices.join(", ")}` : ""} - submitted {ago(j.created_at)}
                          </div>
                          {j.reason && (j.status === "pending" || j.status === "failed") && (
                            <div className="small" style={{ marginTop: 4, color: j.status === "failed" ? "var(--bad)" : "var(--warn)" }}>{j.reason}</div>
                          )}
                        </div>
                        <div className="row">
                          {j.output && <button className="btn ghost sm" onClick={() => setOpen(open === j.id ? null : j.id)}>{open === j.id ? "Hide output" : "Show output"}</button>}
                          {j.has_result && <button className="btn ghost sm" onClick={() => download(`/pool/jobs/${j.id}/result`, `job_${j.id}_result.zip`).catch((e) => setErr(e.message))}>{j.group ? "Download merged results" : "Download results"}</button>}
                          <button className="btn danger sm" onClick={() => remove(j)}>{active ? "Cancel" : "Delete"}</button>
                        </div>
                      </div>
                      {j.group && <Parts j={j} />}
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

// One split job: where each part ran, how long it took, and the time saved by running them side by side
function Parts({ j }) {
  const secs = (v) => (v == null ? "" : v < 90 ? `${v.toFixed(1)} s` : `${(v / 60).toFixed(1)} min`);
  const speed = j.wall_seconds && j.work_seconds ? j.work_seconds / j.wall_seconds : null;
  return (
    <div style={{ marginTop: 10 }}>
      <div className="parts">
        {j.parts.map((p) => {
          const [label, cls] = STATUS[p.status] || [p.status, "chip"];
          return (
            <div key={p.part} className={`part ${p.status}`}>
              <div className="small"><b>Part {p.part}</b></div>
              <div className="small muted clip">{p.device || "no device yet"}</div>
              <div className="row" style={{ gap: 6, marginTop: 4 }}>
                <span className={cls}>{label}</span>
                {p.seconds != null && <span className="small">{secs(p.seconds)}</span>}
              </div>
            </div>
          );
        })}
      </div>
      {j.wall_seconds != null && (
        <div className="small" style={{ marginTop: 8 }}>
          Finished in <b>{secs(j.wall_seconds)}</b> on {j.devices.length} {j.devices.length === 1 ? "device" : "devices"}.
          {" "}The parts used {secs(j.work_seconds)} of computing in total
          {speed && speed > 1.05 ? <>, so the split made it about <b>{speed.toFixed(1)}x faster</b>.</> : "."}
        </div>
      )}
    </div>
  );
}
