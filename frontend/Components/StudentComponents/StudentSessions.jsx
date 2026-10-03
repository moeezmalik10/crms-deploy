import { API_BASE } from "../../src/config";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router";


function getRemainingSeconds(startTime, duration) {

  if (!startTime || !duration) return null;

  const start = new Date(startTime).getTime();
  const end = start + duration * 60 * 1000;
  const now = Date.now();

  return Math.max(0, Math.floor((end - now) / 1000));

}

export default function StudentSessions() {

  const username = localStorage.getItem("username");
  const userId = Number(localStorage.getItem("user_id"));
  const navigate = useNavigate();

  const [sessions, setSessions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState("active");
  const [, setTick] = useState(0);

  // Fetch sessions
  async function fetchSessions() {

    if (!username) return;

    try {

      const res = await fetch(`${API_BASE}/tasks/my/${username}`, {
        headers: {
          Authorization: `Bearer ${localStorage.getItem("token")}`
        }
      });

      const data = await res.json();

      if (res.ok) {
        setSessions(Array.isArray(data) ? data : []);
      }

    } catch (err) {

      console.error("Failed to fetch sessions", err);

    } finally {

      setLoading(false);

    }

  }

  useEffect(() => {

    fetchSessions();

    const poll = setInterval(fetchSessions, 5000);

    return () => clearInterval(poll);

  }, [username]);

  useEffect(() => {

    const timer = setInterval(() => {
      setTick(t => t + 1);
    }, 1000);

    return () => clearInterval(timer);

  }, []);

  async function deleteSession(taskId) {

    if (!window.confirm("Delete this session permanently?")) return;

    try {

      await fetch(`${API_BASE}/tasks/${taskId}`, {
        method: "DELETE",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("token")}`
        },
        body: JSON.stringify({ user_id: userId })
      });

      fetchSessions();

    } catch (err) {

      console.error("Delete failed", err);

    }

  }

  const renderStatus = (status) => {

  switch (status) {

    case "queued":
      return "📌 Queued";

    case "pending":
      return "⏳ Waiting for Resources";

    case "allocated":
      return "🖥️ Machine Allocated";

    case "starting":
      return "⚙️ Initializing Virtual Machine...";

    case "running":
      return "🟢 Running";

    case "completed":
      return "✅ Session Completed";

    case "failed":
      return "❌ Session Failed";

    default:
      return status || "Unknown";

  }

};

  if (loading) {
    return <p className="p-10">Loading sessions...</p>;
  }

  const active = [];
  const history = [];

  sessions.forEach(s => {

  // COMPLETED / FAILED -> HISTORY
  if (
    s.status === "completed" ||
    s.status === "failed"
  ) {

    history.push({
      ...s
    });

    return;
  }

  // EVERYTHING ELSE -> ACTIVE
  const remaining =
    s.status === "running"
      ? getRemainingSeconds(
          s.start_time,
          s.duration_minutes
        )
      : null;

  active.push({
    ...s,
    remaining
  });

});

  const statusStyles = {
    pending: "bg-orange-100 text-orange-600",
    allocated: "bg-indigo-100 text-indigo-700",
    starting: "bg-sky-100 text-sky-600",
    running: "bg-green-100 text-green-700",
    completed: "bg-gray-200 text-gray-700",
    failed: "bg-red-100 text-red-700"
  };

  const renderCard = (s) => (

    <div
      key={s.id}
      className="flex flex-col bg-white justify-center items-center p-3 lg:p-5 rounded-xl hover:-translate-y-1 transition duration-300"
    >

      <div className="flex p-2 gap-4 items-center mb-2">

        <h4 className="md:font-semibold text-sm lg:text-lg">
          {s.task_type}
        </h4>

        <span
          className={`px-3 py-1 rounded-full text-xs lg:font-semibold ${statusStyles[s.status]}`}
        >
          {renderStatus(s.status)}
        </span>

      </div>

      <p className="text-sm">
        <b>Mode:</b> {s.mode}
      </p>

      <p className="text-sm">
        <b>Allocated PC:</b> {s.assigned_pc || "—"}
      </p>

      {/* RUNNING TASK */}
      {s.status === "running" && (
        <>

          <p className="text-sm">
            <b>Time left:</b>{" "}
            {s.remaining !== null
              ? `${Math.ceil(s.remaining / 60)} min`
              : "Calculating..."}
          </p>

          {s.mode === "remote" && (
            <>

              <p className="text-sm">
                <b>Username:</b> {s.vm_username}
              </p>

              <p className="text-sm">
                <b>Password:</b> {s.vm_password}
              </p>

              {s.link && (
                <a
                  href={
                    s.link.startsWith("http")
                      ? s.link
                      : `http://${s.link}`
                  }
                  target="_blank"
                  rel="noopener noreferrer"
                  className="mt-3 mb-3 px-4 py-2 w-[80%] bg-blue-600 hover:bg-blue-700 text-white rounded-md text-center"
                >
                  Connect Now
                </a>
              )}

            </>
          )}

        </>
      )}

      {/* COMPLETED */}
      {s.status === "completed" && (
        <p className="text-gray-600 mt-2">
          Session finished
        </p>
      )}

      {/* FAILED */}
      {s.status === "failed" && (
        <p className="text-red-600 mt-2">
          Session failed
        </p>
      )}

      {/* COMPLETED TIME */}
      {s.completed_at && (
        <p className="text-sm">
          <b>Completed:</b>{" "}
          {new Date(s.completed_at).toLocaleString()}
        </p>
      )}

      {/* ML RESULTS */}
      {s.task_type === "ml_job_parent" &&
        s.status === "completed" && (
          <button
            onClick={() =>
              navigate(`/studentpage/mltaskresults/${s.id}`)
            }
            className="mt-3 px-4 py-2 w-[80%] bg-blue-600 hover:bg-blue-700 text-white rounded-md"
          >
            View Results
          </button>
        )}

      {/* DELETE */}
      <button
        onClick={() => deleteSession(s.id)}
        className="mt-3 mb-3 px-4 py-2 w-[80%] bg-red-500 hover:bg-red-600 text-white rounded-md"
      >
        Delete
      </button>

    </div>

  );

  return (

    <div className="p-6 flex flex-col">

      {/* TABS */}
      <div className="flex gap-5 mb-8">

        <button
          onClick={() => setActiveTab("active")}
          className={`px-6 py-2 rounded-lg font-semibold transition ${
            activeTab === "active"
              ? "bg-blue-600 text-white shadow"
              : "bg-gray-200 hover:bg-gray-300"
          }`}
        >
          Active
        </button>

        <button
          onClick={() => setActiveTab("history")}
          className={`px-6 py-2 rounded-lg font-semibold transition ${
            activeTab === "history"
              ? "bg-blue-600 text-white shadow"
              : "bg-gray-200 hover:bg-gray-300"
          }`}
        >
          History
        </button>

      </div>

      {/* CARDS */}
      <div className="grid rounded-xl gap-6 grid-cols-1 sm:grid-cols-2 lg:grid-cols-2 xl:grid-cols-3">

        {activeTab === "active" &&
          (active.length === 0
            ? <p>No active sessions</p>
            : active.map(renderCard))}

        {activeTab === "history" &&
          (history.length === 0
            ? <p>No session history</p>
            : history.map(renderCard))}

      </div>

    </div>

  );

}