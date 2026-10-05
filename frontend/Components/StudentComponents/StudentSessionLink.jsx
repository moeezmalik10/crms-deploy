import { API_BASE } from "../../src/config";
import { useEffect, useState } from "react";

// Remote Link: shows how to reach the PC of the student's latest Remote session.
// RustDesk sessions (link "rustdesk:<id>") show the RustDesk ID and a one-time password
// that only works while the session runs; older VM sessions show their browser link.

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
    allocated: "PC found - preparing remote access",
    starting: "Preparing remote access...",
    running: "Remote access ready",
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
          <h4 className="text-lg font-semibold">No remote session yet</h4>
          <p className="text-sm text-gray-600 mt-2">Make a New Request with access mode <b>Remote</b> to use a lab PC from your own laptop or phone.</p>
        </div>
      </div>
    );
  }

  const isRustDesk = (session.link || "").startsWith("rustdesk:");
  const rdId = isRustDesk ? session.link.slice("rustdesk:".length) : null;
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

        {session.status === "running" && isRustDesk && (
          <>
            <CopyField label="RustDesk ID of the lab PC" value={rdId} />
            {session.vm_password ? (
              <CopyField label="One-time password (works only during this session)" value={session.vm_password} />
            ) : (
              <p className="text-sm text-red-700">Log out and log in again to see the password.</p>
            )}
            <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 text-sm text-blue-900">
              <p className="font-semibold mb-2">How to connect</p>
              <ol className="list-decimal ml-5 flex flex-col gap-1">
                <li>Install RustDesk on your laptop or phone (free): <a className="underline" href="https://rustdesk.com/download" target="_blank" rel="noopener noreferrer">rustdesk.com/download</a></li>
                <li>Open RustDesk, type the <b>ID</b> above in "Control Remote Desktop" and press <b>Connect</b>.</li>
                <li>Enter the <b>one-time password</b>. You now control the lab PC.</li>
                <li>When the time is up the connection is closed and the password stops working.</li>
              </ol>
            </div>
          </>
        )}

        {session.status === "running" && !isRustDesk && (
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
          <p className="text-sm text-gray-600">This page updates by itself. The ID and password appear here as soon as the lab PC is ready.</p>
        )}
      </div>
    </div>
  );
}
