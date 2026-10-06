import { API_BASE } from "../../src/config";
import { useEffect, useState } from "react";

// Remote Link: shows how to reach the PC of the student's latest Remote session.
// Workspace sessions show the private https address and a one-time password of a sandboxed
// VS Code that runs on a pool device; older VM sessions show their browser link.

function CopyField({ label, value }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try { await navigator.clipboard.writeText(value); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { /* ignore */ }
  };
  return (
    <div className="w-full">
      <div className="text-xs text-gray-500 mb-1">{label}</div>
      <div className="flex items-center gap-2">
        <div className="flex-1 bg-gray-100 rounded-lg px-3 py-2 font-mono text-lg tracking-wider break-all">{value}</div>
        <button onClick={copy} className="px-3 py-2 rounded-lg bg-gray-800 text-white text-sm shrink-0">
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
    </div>
  );
}

export default function StudentRemoteLink() {
  const username = localStorage.getItem("username");
  const [session, setSession] = useState(null);
  const [loading, setLoading] = useState(true);
  const [now, setNow] = useState(Date.now());

  async function fetchSession() {
    if (!username) return;
    try {
      const res = await fetch(`${API_BASE}/tasks/my/${username}`, {
        headers: { Authorization: `Bearer ${localStorage.getItem("token")}` },
      });
      const data = await res.json();
      if (Array.isArray(data)) {
        const remoteTask = data.filter((s) => s.mode === "remote").sort((a, b) => b.id - a.id)[0];
        setSession(remoteTask || null);
      }
    } catch (err) {
      console.error("Failed to fetch remote session", err);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchSession();
    const interval = setInterval(fetchSession, 5000);
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => { clearInterval(interval); clearInterval(tick); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [username]);

  const statusStyles = {
    pending: "bg-orange-100 text-orange-600",
    allocated: "bg-sky-100 text-sky-600",
    starting: "bg-sky-100 text-sky-600",
    running: "bg-green-100 text-green-700",
    completed: "bg-gray-200 text-gray-700",
    failed: "bg-red-100 text-red-700",
  };

  const statusText = {
    pending: "Waiting for a free PC",
    allocated: "Device found - preparing your workspace",
    starting: "Preparing your workspace...",
    running: "Workspace ready",
    completed: "Session finished",
    failed: "Session failed",
  };

  if (loading) {
    return (
      <div className="p-10 bg-gray-100 flex justify-center items-center w-full">
        <div className="bg-white p-6 rounded-xl shadow-lg">
          <h4 className="text-lg font-semibold animate-pulse">Checking remote session...</h4>
        </div>
      </div>
    );
  }

  if (!session) {
    return (
      <div className="p-10 bg-gray-100 flex justify-center items-center w-full">
        <div className="bg-white p-6 rounded-xl shadow-lg text-center max-w-md">
          <h4 className="text-lg font-semibold">No workspace yet</h4>
          <p className="text-sm text-gray-600 mt-2">Make a New Request with access mode <b>Workspace</b> to get a sandboxed slice of a pool device that you use from your browser.</p>
        </div>
      </div>
    );
  }

  const isWorkspace = session.vm_username === "workspace" || (session.link || "").includes("trycloudflare.com");
  const ends = session.expiry_time ? new Date(session.expiry_time).getTime() : null;
  const left = ends ? Math.max(0, Math.round((ends - now) / 1000)) : null;

  return (
    <div className="p-4 md:p-8 w-full">
      <div className="max-w-xl mx-auto bg-white rounded-xl shadow p-5 md:p-6 flex flex-col gap-4 text-left">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h4 className="font-semibold text-lg">{session.task_type || "Remote session"}</h4>
          <span className={`px-3 py-1 rounded-full text-xs font-semibold ${statusStyles[session.status] || ""}`}>
            {statusText[session.status] || session.status}
          </span>
        </div>

        <div className="text-sm text-gray-700">
          <b>Lab PC:</b> {session.assigned_pc || "-"}
          {session.status === "running" && left !== null && (
            <span className="ml-4"><b>Time left:</b> {Math.floor(left / 60)}:{String(left % 60).padStart(2, "0")}</span>
          )}
        </div>

        {session.status === "running" && isWorkspace && (
          <>
            <CopyField label="Workspace address" value={session.link} />
            {session.vm_password ? (
              <CopyField label="Password (works only during this session)" value={session.vm_password} />
            ) : (
              <p className="text-sm text-red-700">Log out and log in again to see the password.</p>
            )}
            <a href={session.link} target="_blank" rel="noopener noreferrer"
               className="px-4 py-2 bg-gray-900 hover:bg-black text-white rounded-md text-center font-semibold">
              Open workspace
            </a>
            <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 text-sm text-blue-900">
              <p className="font-semibold mb-2">How it works</p>
              <ul className="list-disc ml-5 flex flex-col gap-1">
                <li>The workspace is VS Code in your browser, running in a sandbox on {session.assigned_pc}. It has C/C++ and Python.</li>
                <li>It uses only the CPU, memory and disk slice given to this request. The PC owner keeps working normally and cannot see your files.</li>
                <li>Download anything you want to keep before the time is up: the workspace and its files are deleted when the session ends.</li>
              </ul>
            </div>
          </>
        )}

        {session.status === "running" && !isWorkspace && (
          <>
            <p className="text-sm"><b>Username:</b> {session.vm_username}</p>
            <p className="text-sm"><b>Password:</b> {session.vm_password || "(log in again to see it)"}</p>
            {session.link && (
              <a
                href={session.link.startsWith("http") ? session.link : `http://${session.link}`}
                target="_blank"
                rel="noopener noreferrer"
                className="mt-2 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-md text-center"
              >
                Connect Now
              </a>
            )}
          </>
        )}

        {(session.status === "pending" || session.status === "allocated" || session.status === "starting") && (
          <p className="text-sm text-gray-600">This page updates by itself. The address and password appear here as soon as the workspace is ready (the first one on a device can take a few minutes).</p>
        )}
      </div>
    </div>
  );
}
