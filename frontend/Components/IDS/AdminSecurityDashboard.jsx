import React, { useEffect, useState } from "react";
import jsPDF from "jspdf";
import autoTable from "jspdf-autotable";
import { supabase } from "../../src/supabaseClient";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  ResponsiveContainer,
  Cell
} from "recharts";
export default function AdminSecurityDashboard() {
  const [logs, setLogs] = useState([])
  const [chartData, setChartData] = useState([])
  const [selectedAttack, setSelectedAttack] = useState("All")
  const filteredLogs =
  selectedAttack === "All"
    ? logs
    : logs.filter((log) => log.attack_type === selectedAttack)

  useEffect(() => {
    fetchLogs();

    const interval = setInterval(() => {
      fetchLogs();
    }, 5000); // refresh every 5 sec

    return () => clearInterval(interval);
  }, []);

  const fetchLogs = async () => {
    const { data, error } = await supabase
      .from("intrusion_logs")
      .select("*")
      .order("created_at", { ascending: false });

    if (error) {
      console.log("Supabase Error:", error.message);
    } else {
      setLogs(data);
    }
  };

  const generateChart = (data) => {
  const filteredData =
    selectedAttack === "All"
      ? data
      : data.filter(
          (log) => log.attack_type === selectedAttack
        );

  const counts = {};

  filteredData.forEach((log) => {
    counts[log.attack_type] =
      (counts[log.attack_type] || 0) + 1;
  });

  const formatted = Object.keys(counts).map((type) => ({
    attack: type,
    count: counts[type]
  }));

  setChartData(formatted);
}

useEffect(() => {
  generateChart(logs);
}, [selectedAttack, logs])


const downloadPDF = () => {
  const doc = new jsPDF();

  const reportDate = new Date().toLocaleString("en-PK", {
    timeZone: "Asia/Karachi"
  });

  const uniqueAttackTypes = new Set(
    logs.map((log) => log.attack_type)
  ).size;

  // Header
  doc.setFontSize(18);
  doc.text("INTELLIGENT THREAT DEFENSE SYSTEM", 14, 20);

  doc.setFontSize(14);
  doc.text("Security Incident Report", 14, 30);

  // Report info
  doc.setFontSize(11);
  doc.text(`Generated: ${reportDate}`, 14, 40);
  doc.text(`Total Incidents: ${logs.length}`, 14, 48);
  doc.text(`Unique Attack Types: ${uniqueAttackTypes}`, 14, 56);

  // Table
  autoTable(doc, {
    head: [["Email", "Attack Type", "Time", "IP", "Country", "City"]],
    body: logs.map((log) => [
      log.email,
      log.attack_type,
      new Date(log.created_at).toLocaleString("en-PK", {
        timeZone: "Asia/Karachi"
      }),
      log.ip_address,
      log.country,
      log.city
    ]),
    startY: 65
  });

  doc.save("security_report.pdf");
};
const attackColors = {
  "SQL Injection Attempt": "#ef4444",
  "XSS Attempt": "#f59e0b",
  "Blocked: IP Blacklisted": "#8b5cf6",
  "BENIGN": "#10b981"
}
const attackTypes = [
  "All",
  ...new Set(logs.map((log) => log.attack_type))
]
  return (
    <div className="p-6">
        <div className="mb-6 flex justify-end">
  <button
    onClick={downloadPDF}
    className="px-5 py-3 border rounded hover:bg-[#2e632f] bg-[#4caf50]"
  >
    Download Security Report
  </button>
</div>

      <h1 className="text-3xl font-bold mb-8 text-center">
        Security Dashboard
      </h1>

      {/* Chart Section */}
      <div className="bg-white p-6 rounded-xl shadow mb-10 h-87.5">
        <h2 className="text-xl font-semibold mb-4">Attack Statistics</h2>

        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="attack" />
            <YAxis />
            <Tooltip />
            <Bar dataKey="count">
  {chartData.map((entry, index) => (
    <Cell
      key={`cell-${index}`}
      fill={attackColors[entry.attack] || "#3b82f6"}
    />
  ))}
</Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>

    {/* Table Section */}
<div className="bg-white p-6 rounded-xl shadow">
  <div className="flex justify-between items-center mb-4">
  <h2 className="text-xl font-semibold">Recent Intrusions</h2>

  <select
    value={selectedAttack}
    onChange={(e) => setSelectedAttack(e.target.value)}
    className="border px-3 py-2 rounded"
  >
    {attackTypes.map((type, index) => (
      <option key={index} value={type}>
        {type}
      </option>
    ))}
  </select>
</div>

  <div className="overflow-x-auto">
    <table className="min-w-full border border-gray-300">
      <thead>
        <tr className="border-b">
          <th className="p-3 text-left whitespace-nowrap">Email</th>
          <th className="p-3 text-left whitespace-nowrap">Attack Type</th>
          <th className="p-3 text-left whitespace-nowrap">Time</th>
          <th className="p-3 text-left whitespace-nowrap">IP Address</th>
          <th className="p-3 text-left whitespace-nowrap">Country</th>
          <th className="p-3 text-left whitespace-nowrap">City</th>
        </tr>
      </thead>

      <tbody>
        {filteredLogs.map((log) => (
          <tr key={log.id} className="border-b">
            <td className="p-3 whitespace-nowrap">{log.email}</td>
            <td className="p-3 whitespace-nowrap">{log.attack_type}</td>
            <td className="p-3 whitespace-nowrap">
              {new Intl.DateTimeFormat("en-PK", {
                timeZone: "Asia/Karachi",
                dateStyle: "short",
                timeStyle: "medium"
              }).format(new Date(log.created_at))}
            </td>
            <td className="p-3 whitespace-nowrap">{log.ip_address}</td>
            <td className="p-3 whitespace-nowrap">{log.country}</td>
            <td className="p-3 whitespace-nowrap">{log.city}</td>
          </tr>
        ))}
      </tbody>
    </table>
  </div>
</div>

    </div>
  );
}