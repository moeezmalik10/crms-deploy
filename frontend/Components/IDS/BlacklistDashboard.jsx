import { IDS_BASE } from "../../src/config";
import React, { useEffect, useState } from "react";

export default function BlacklistDashboard() {
  const [blockedIPs, setBlockedIPs] = useState([]);

  useEffect(() => {
    fetchBlockedIPs();
  }, []);

  const fetchBlockedIPs = async () => {
    try {
      const res = await fetch(
        `${IDS_BASE}/blacklisted`
      );

      const data = await res.json();
      setBlockedIPs(data);
    } catch (error) {
      console.log("Error fetching blacklisted IPs:", error);
    }
  };

  const unblockIP = async (ip) => {
    try {
      await fetch(`${IDS_BASE}/unblock`, {
        method: "DELETE",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          ip_address: ip
        })
      });

      fetchBlockedIPs();
    } catch (error) {
      console.log("Error unblocking IP:", error);
    }
  };

  return (
    <div className="p-6">
      <h1 className="text-3xl font-bold mb-6">
        Blacklisted IP Dashboard
      </h1>

      {/* Responsive Scroll Wrapper */}
      <div className="overflow-x-auto">
        <table className="min-w-full border border-gray-300">
          <thead>
            <tr className="border-b">
              <th className="p-3 text-left whitespace-nowrap">
                IP Address
              </th>
              <th className="p-3 text-left whitespace-nowrap">
                Blocked Time
              </th>
              <th className="p-3 text-left whitespace-nowrap">
                Action
              </th>
            </tr>
          </thead>

          <tbody>
            {blockedIPs.length > 0 ? (
              blockedIPs.map((ip) => (
                <tr key={ip.id} className="border-b">
                  <td className="p-3 whitespace-nowrap">
                    {ip.ip_address}
                  </td>

                  <td className="p-3 whitespace-nowrap">
                    {new Intl.DateTimeFormat("en-PK", {
                      timeZone: "Asia/Karachi",
                      dateStyle: "short",
                      timeStyle: "medium"
                    }).format(new Date(ip.created_at))}
                  </td>

                  <td className="p-3 whitespace-nowrap">
                    <button
                      onClick={() => unblockIP(ip.ip_address)}
                      className="px-4 py-2 border rounded bg-red-500 hover:bg-red-600 border-none"
                    >
                      Unblock
                    </button>
                  </td>
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan="3" className="p-4 text-center">
                  No blacklisted IPs
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}