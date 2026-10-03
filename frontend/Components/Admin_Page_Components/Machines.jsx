import { API_BASE } from "../../src/config";
import { useEffect, useState } from "react";
import {
  PieChart,
  Pie,
  Cell,
  Tooltip,
  Legend,
  ResponsiveContainer
} from "recharts";


/* =========================
   DONUT CHART COMPONENT
========================= */
function DonutChart({ title, data, colors }) {
  return (
    <div className="w-[250px] h-[250px]">
      <h4 className="text-center font-semibold mb-4">{title}</h4>

      <ResponsiveContainer width="100%" height={260}>
        <PieChart>
          <Pie
            data={data}
            innerRadius={60}
            outerRadius={90}
            dataKey="value"
          >
            {data.map((_, index) => (
              <Cell key={index} fill={colors[index]} />
            ))}
          </Pie>

          <Tooltip />
          <Legend verticalAlign="bottom" align="center" />
        </PieChart>
      </ResponsiveContainer>
    </div>
  );
}

/* =========================
   MACHINES PAGE
========================= */
export default function Machines() {

  const [machines, setMachines] = useState([]);
  const [loading, setLoading] = useState(true);

  // 🔥 Fetch Machines
  const fetchMachines = async () => {
    try {
      const res = await fetch(`${API_BASE}/admin/nodes`, {
        headers: {
          Authorization: `Bearer ${localStorage.getItem("token")}`
        }
      });

      const data = await res.json();
      setMachines(Array.isArray(data) ? data : []);

    } catch (err) {
      console.error("Failed to load machines", err);
    } finally {
      setLoading(false);
    }
  };

  // 🔥 Delete Node
  const handleDeleteNode = async (nodeId) => {
    const confirmDelete = window.confirm("Delete this node?");

    if (!confirmDelete) return;

    try {
      const res = await fetch(`${API_BASE}/admin/nodes/${nodeId}`, {
        method: "DELETE",
        headers: {
          Authorization: `Bearer ${localStorage.getItem("token")}`
        }
      });

      const data = await res.json();

      if (!res.ok) {
        alert(data.error || "Delete failed");
        return;
      }

      // Refresh list after delete
      fetchMachines();

    } catch (err) {
      console.error("Delete failed", err);
      alert("Server error");
    }
  };

  useEffect(() => {
    fetchMachines();
    const interval = setInterval(fetchMachines, 5000);
    return () => clearInterval(interval);
  }, []);

  if (loading) {
    return <p className="p-6">Loading machines...</p>;
  }

  const totalMachines = machines.length;

  const busyMachines = machines.filter(
    m => (m.running_tasks ?? 0) > 0
  ).length;

  const idleMachines = totalMachines - busyMachines;

  const totalCpuUsed = machines.reduce(
    (sum, m) => sum + (Number(m.cpu_used) || 0),
    0
  );

  const totalCpuFree = machines.reduce(
    (sum, m) => sum + (Number(m.cpu_free) || 0),
    0
  );

  const totalRamUsed = machines.reduce(
    (sum, m) => sum + (Number(m.ram_used) || 0),
    0
  );

  const totalRamFree = machines.reduce(
    (sum, m) => sum + (Number(m.ram_free) || 0),
    0
  );

  const hasCpuData = totalCpuUsed + totalCpuFree > 0;
  const hasRamData = totalRamUsed + totalRamFree > 0;

  return (
    <div className="p-6">

      {machines.length === 0 && (
        <p>No machines registered.</p>
      )}

      {/* DASHBOARD SUMMARY */}
      <div className="mt-8 mb-14 pb-5 border-b overflow-x-auto overflow-y-hidden custom-scrollbar">
        <div className="flex flex-nowrap gap-8 min-w-max items-start px-2">

          <DonutChart
            title="PC Status"
            data={[
              { name: "Idle", value: idleMachines },
              { name: "Busy", value: busyMachines }
            ]}
            colors={["#4caf50", "#f44336"]}
          />

          <DonutChart
            title="CPU Load (%)"
            data={
              hasCpuData
                ? [
                    { name: "Used (%)", value: totalCpuUsed },
                    { name: "Free (%)", value: totalCpuFree }
                  ]
                : [{ name: "No Data", value: 1 }]
            }
            colors={hasCpuData ? ["#ff9800", "#c8e6c9"] : ["#e0e0e0"]}
          />

          <DonutChart
            title="RAM Usage (MB)"
            data={
              hasRamData
                ? [
                    { name: "Used (MB)", value: totalRamUsed },
                    { name: "Free (MB)", value: totalRamFree }
                  ]
                : [{ name: "No Data", value: 1 }]
            }
            colors={hasRamData ? ["#3f51b5", "#bbdefb"] : ["#e0e0e0"]}
          />

        </div>
      </div>

      {/* MACHINE GRID */}
      <div className="grid gap-5 grid-cols-1 md:grid-cols-2 xl:grid-cols-3 w-full">
        {machines.map(m => (

          <div
            key={m.id}
            className="border border-gray-300 rounded-lg p-4 bg-white shadow-sm"
          >

            <h3 className="mb-2 font-semibold">{m.name}</h3>

            <p>
              Status:{" "}
              <span
                className={
                  m.status === "online"
                    ? "text-green-600 font-semibold"
                    : "text-red-600 font-semibold"
                }
              >
                {m.status}
              </span>
            </p>

            <p>IP: {m.ip}</p>

            <hr className="my-3" />

            <div className="space-y-1">
              {m.cpu_used != null && m.cpu_free != null ? (
                <>
                  <p>CPU Used: {Number(m.cpu_used).toFixed(1)}%</p>
                  <p>CPU Free: {Number(m.cpu_free).toFixed(1)}%</p>
                  <p>RAM Used: {Math.round(m.ram_used ?? 0)} MB</p>
                  <p>RAM Free: {Math.round(m.ram_free ?? 0)} MB</p>
                </>
              ) : (
                <p className="text-gray-400 italic">
                  Waiting for heartbeat…
                </p>
              )}
            </div>

            <hr className="my-3" />

            <p
              className={
                (m.running_tasks ?? 0) > 0
                  ? "text-red-500 font-semibold"
                  : "text-green-600 font-semibold"
              }
            >
              {(m.running_tasks ?? 0) > 0
                ? "🔴 Busy"
                : "🟢 Idle"} ({m.running_tasks ?? 0} tasks)
            </p>

            <div className="text-gray-500 text-sm mt-2">
              Last heartbeat:{" "}
              {m.last_seen
                ? new Date(m.last_seen).toLocaleString()
                : "N/A"}
            </div>

            {/* 🔥 DELETE BUTTON */}
            <button
              onClick={() => handleDeleteNode(m.id)}
              className="mt-4 px-4 py-2 bg-red-500 hover:bg-red-600 text-white rounded-md text-sm w-full"
            >
              Delete Node
            </button>

          </div>

        ))}
      </div>

    </div>
  );
}