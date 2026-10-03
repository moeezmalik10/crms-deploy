import { API_BASE } from "../../src/config";
import { useEffect, useState } from "react";


export default function StudentRemoteLink() {

  const username = localStorage.getItem("username");

  const [session, setSession] = useState(null);
  const [loading, setLoading] = useState(true);

  async function fetchSession() {

    if (!username) return;

    try {

      const res = await fetch(`${API_BASE}/tasks/my/${username}`);
      const data = await res.json();

      if (Array.isArray(data)) {

        const remoteTask = data
          .filter((s) => s.mode === "remote")
          .sort((a, b) => b.id - a.id)[0];

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

    return () => clearInterval(interval);

  }, [username]);

  const statusStyles = {
    pending: "bg-orange-100 text-orange-600",
    starting: "bg-sky-100 text-sky-600",
    running: "bg-green-100 text-green-700",
    completed: "bg-gray-200 text-gray-700",
    failed: "bg-red-100 text-red-700"
  };

  const renderStatus = () => {

    switch (session?.status) {

      case "pending":
        return "⏳ Task Queued - Waiting for Resources";

      case "starting":
        return "⚙️ Initializing Virtual Machine...";

      case "running":
        return "🟢 Running";

      case "completed":
        return "✅ Session Completed";

      case "failed":
        return "❌ Session Failed";

      default:
        return "Unknown";

    }
  };

  if (loading) {
    return (
      <div className="p-10 bg-gray-100 flex justify-center items-center">
        <div className="bg-white p-6 rounded-xl shadow-lg">
          <h4 className="text-lg font-semibold animate-pulse">
            Checking Remote Session...
          </h4>
        </div>
      </div>
    );
  }

  if (!session) {
    return (
      <div className="p-10 bg-gray-100  flex justify-center items-center">
        <div className="bg-white p-6 rounded-xl shadow-lg">
          <h4 className="text-lg font-semibold">
            No Remote Request Found
          </h4>
        </div>
      </div>
    );
  }

 return (
  <div className="p-4 flex flex-col">

    <div className="grid rounded-xl gap-6 grid-cols-1 ">

      <div
        className="flex flex-col bg-white justify-center items-center p-2 lg:p-5 rounded-xl hover:-translate-y-1 transition duration-300"
      >

        <div className="flex p-2 gap-4 items-center mb-2">

          <h4 className="md:font-semibold text-sm lg:text-lg">
            {session.task_type || "Remote Task"}
          </h4>

          <span
            className={`px-3 py-1 rounded-full text-xs lg:font-semibold ${statusStyles[session.status]}`}
          >
            {renderStatus()}
          </span>

        </div>

        <p className="text-sm">
          <b>Mode:</b> remote
        </p>

        <p className="text-sm">
          <b>Allocated PC:</b> {session.assigned_pc || "—"}
        </p>

        {session.status === "running" && (
          <>
            <p className="text-sm">
              <b>Username:</b> {session.vm_username}
            </p>

            <p className="text-sm">
              <b>Password:</b> {session.vm_password}
            </p>

           {session.link && (
  <a
    href={
      session.link.startsWith("http")
        ? session.link
        : `http://${session.link}`
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

        {session.status === "completed" && (
          <p className="text-gray-600 mt-2">
            Session finished
          </p>
        )}

      </div>

    </div>

  </div>
);
}