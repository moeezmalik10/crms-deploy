import { useEffect, useRef, useState } from "react";
import { api, ago, bytes, download } from "./PoolUI";

// Files kept on contributors' disks: encrypted, split into parts, two copies on two devices.
export default function Storage() {
  const [files, setFiles] = useState([]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const ref = useRef();

  async function load() {
    try { setFiles(await api("/pool/storage")); } catch (e) { setErr(e.message); }
  }
  useEffect(() => { load(); const i = setInterval(load, 5000); return () => clearInterval(i); }, []);

  async function upload(file) {
    if (!file) return;
    setBusy(true); setErr("");
    const fd = new FormData(); fd.append("file", file);
    try { await api("/pool/storage", { method: "POST", body: fd }); load(); } catch (e) { setErr(e.message); }
    setBusy(false);
    if (ref.current) ref.current.value = "";
  }

  async function act(f, what) {
    setErr("");
    try {
      if (what === "prepare") await api(`/pool/storage/${f.id}/prepare`, { method: "POST" });
      if (what === "download") await download(`/pool/storage/${f.id}/download`, f.name);
      if (what === "delete") {
        if (!window.confirm(`Delete ${f.name}? The copies on the devices are removed too.`)) return;
        await api(`/pool/storage/${f.id}`, { method: "DELETE" });
      }
      load();
    } catch (e) { setErr(e.message); }
  }

  return (
    <div className="pool">
      <div className="wrap">
        <div>
          <h1>Pool storage</h1>
          <p className="lead">
            Keep files on space that other students lend. Each file is encrypted before it leaves the server, split into
            parts, and stored twice on two different PCs, so it stays available when one of them is switched off.
          </p>
        </div>
        {err && <div className="err">{err}</div>}

        <div className="panel row">
          <input ref={ref} type="file" onChange={(e) => upload(e.target.files[0])} disabled={busy} />
          <span className="small muted">{busy ? "Encrypting and uploading..." : "Files up to 25 MB."}</span>
        </div>

        <section>
          <h2>My files</h2>
          <div className="panel">
            {files.length === 0 ? <p className="muted" style={{ margin: 0 }}>No files yet. Choose a file above to store it in the pool.</p> : (
              <div className="list">
                {files.map((f) => {
                  const waiting = f.chunks_ready > 0 && !f.download_ready;
                  return (
                    <div className="item" key={f.id}>
                      <div className="between">
                        <div>
                          <div className="row">
                            <b>{f.name}</b><span className="small muted">{bytes(f.size)}</span>
                            {f.status === "stored"
                              ? <span className="chip good">{f.copies} copies on devices</span>
                              : <span className="chip warn">Copying to devices ({f.copies} of {f.copies_wanted})</span>}
                            {!f.available && <span className="chip">A device holding it is offline</span>}
                          </div>
                          <div className="small muted" style={{ marginTop: 4 }}>
                            Kept on {f.devices.length ? f.devices.join(" and ") : "the server until a device is free"}
                            {f.server_backup ? ", with a server copy until both device copies are in place" : ""} - added {ago(f.created_at)}
                          </div>
                          {waiting && <div className="small" style={{ marginTop: 4 }}>Collecting parts from the devices: {f.chunks_ready} of {f.chunks}</div>}
                        </div>
                        <div className="row">
                          {f.download_ready
                            ? <button className="btn primary sm" onClick={() => act(f, "download")}>Download</button>
                            : <button className="btn ghost sm" disabled={!f.available || waiting} onClick={() => act(f, "prepare")}>{waiting ? "Preparing..." : "Prepare download"}</button>}
                          <button className="btn danger sm" onClick={() => act(f, "delete")}>Delete</button>
                        </div>
                      </div>
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
