import { API_BASE } from "../../src/config";
import { useState, useEffect } from "react";
import { useNavigate } from "react-router";


export default function History() {
  const [activeTab, setActiveTab] = useState("pending");
  const [sessions, setSessions] = useState([]);
  const navigate = useNavigate();

  /* Protect Admin Route */
  useEffect(() => {
    const role = localStorage.getItem("role");
    if (role !== "admin") {
      navigate("/");
    }
  }, [navigate]);

  /* Fetch Sessions */
  useEffect(() => {
    fetchSessions();
    const interval = setInterval(fetchSessions, 5000);
    return () => clearInterval(interval);
  }, []);

  const fetchSessions = async () => {
    try {
      const response = await fetch(`${API_BASE}/admin-sessions`, {
        headers: {
          Authorization: `Bearer ${localStorage.getItem("token")}`
        }
      });

      const data = await response.json();
      setSessions(data.sessions || []);
    } catch (err) {
      console.error("Error fetching sessions:", err);
    }
  };

  const handleDelete = async (taskId) => {
    const confirmDelete = window.confirm(
      "Are you sure you want to delete this session?"
    );

    if (!confirmDelete) return;

    try {
      const res = await fetch(`${API_BASE}/admin/tasks/${taskId}`, {
        method: "DELETE",
        headers: {
          Authorization: `Bearer ${localStorage.getItem("token")}`
        }
      });

      if (res.ok) {
        fetchSessions();
      } else {
        alert("Failed to delete session");
      }
    } catch (err) {
      console.error("Delete failed:", err);
    }
  };

  /* Filters */
  const pendingSessions = sessions.filter(
    (s) => s.status === "pending" || s.status === "starting"
  );

  const runningSessions = sessions.filter(
    (s) => s.status === "running"
  );

  const historySessions = sessions.filter(
    (s) => s.status === "completed" || s.status === "failed"
  );

  /* Status Badge */
  const statusStyle = (status) => {
    switch (status) {
      case "pending":
        return "bg-orange-100 text-orange-600";
      case "starting":
        return "bg-sky-100 text-sky-600";
      case "running":
        return "bg-green-100 text-green-700";
      case "completed":
        return "bg-gray-200 text-gray-700";
      case "failed":
        return "bg-red-100 text-red-700";
      default:
        return "bg-gray-100 text-gray-600";
    }
  };

  /* Desktop Table */
  const renderTable = (data) => (
    <div className="hidden md:block bg-white rounded-xl shadow-lg overflow-hidden ">
      <table className="w-full text-center">
        <thead className="bg-gray-800 text-white">
          <tr>
            <th className="py-3 px-4">Task ID</th>
            <th className="py-3 px-4">Student</th>
            <th className="py-3 px-4">Machine</th>
            <th className="py-3 px-4">Start Time</th>
            <th className="py-3 px-4">Status</th>
            <th className="py-3 px-4">Action</th>
          </tr>
        </thead>

        <tbody>
          {data.length === 0 ? (
            <tr>
              <td colSpan="6" className="py-6 text-gray-500">
                No sessions found
              </td>
            </tr>
          ) : (
            data.map((s) => (
              <tr
                key={s.task_id}
                className="border-b hover:bg-gray-50 transition"
              >
                <td className="py-3">{s.task_id}</td>
                <td>{s.student_name}</td>
                <td>{s.machine}</td>
                <td>{s.start_time}</td>

                <td>
                  <span
                    className={`px-3 py-1 rounded-full text-xs font-semibold capitalize ${statusStyle(
                      s.status
                    )}`}
                  >
                    {s.status}
                  </span>
                </td>

                <td>
                  <button
                    onClick={() => handleDelete(s.task_id)}
                    className="bg-red-500 hover:bg-red-600 text-white px-3 py-1 rounded-md text-sm"
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );

  /* Mobile Cards */
  const renderCards = (data) => (
    <div className="md:hidden grid gap-4">
      {data.length === 0 ? (
        <p className="text-center text-gray-500">No sessions found</p>
      ) : (
        data.map((s) => (
          <div
            key={s.task_id}
            className="bg-white p-4 rounded-xl shadow-md flex flex-col gap-2"
          >
            <div className="flex justify-between items-center">
              <span className="font-semibold text-sm">
                Task #{s.task_id}
              </span>

              <span
                className={`px-2 py-1 rounded-full text-xs font-semibold capitalize ${statusStyle(
                  s.status
                )}`}
              >
                {s.status}
              </span>
            </div>

            <p className="text-sm">
              <b>Student:</b> {s.student_name}
            </p>

            <p className="text-sm">
              <b>Machine:</b> {s.machine}
            </p>

            <p className="text-sm">
              <b>Start Time:</b> {s.start_time}
            </p>

            <button
              onClick={() => handleDelete(s.task_id)}
              className="bg-red-500 hover:bg-red-600 text-white px-3 py-1 rounded-md text-sm mt-2"
            >
              Delete
            </button>
          </div>
        ))
      )}
    </div>
  );

  return (


<div className="p-4 md:p-6 bg-gray-100 min-h-screen ">
      {/* Tabs */}
      <nav className="flex gap-5 mb-8 flex-wrap">

        <button
          onClick={() => setActiveTab("pending")}
          className={`px-6 py-2 rounded-lg font-semibold transition ${
            activeTab === "pending"
              ? "bg-blue-600 text-white shadow"
              : "bg-gray-200 hover:bg-gray-300"
          }`}
        >
          Pending
        </button>

        <button
          onClick={() => setActiveTab("running")}
          className={`px-6 py-2 rounded-lg font-semibold transition ${
            activeTab === "running"
              ? "bg-blue-600 text-white shadow"
              : "bg-gray-200 hover:bg-gray-300"
          }`}
        >
          Running
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

      </nav>

      {/* Pending */}
      {activeTab === "pending" && (
        <>
          {renderCards(pendingSessions)}
          {renderTable(pendingSessions)}
        </>
      )}

      {/* Running */}
      {activeTab === "running" && (
        <>
          {renderCards(runningSessions)}
          {renderTable(runningSessions)}
        </>
      )}

      {/* History */}
      {activeTab === "history" && (
        <>
          {renderCards(historySessions)}
          {renderTable(historySessions)}
        </>
      )}

    </div>
  );
}