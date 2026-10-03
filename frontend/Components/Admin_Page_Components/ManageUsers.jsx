import { API_BASE, IDS_BASE } from "../../src/config";
import { useEffect, useState } from "react";


export default function ManageUsers() {

  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);

  const [formData, setFormData] = useState({
    email: "",
    password: "",
    role: "student",
  });

  const token = localStorage.getItem("token");

  // ================= FETCH USERS =================

const fetchUsers = async () => {
  try {
    const res = await fetch(`${API_BASE}/admin/users`, {
      method: "GET",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`
      }
    });

    if (!res.ok) {
  const errorText = await res.text();
  console.log("Server Error:", errorText);
  throw new Error(errorText);
}

    const data = await res.json();
    setUsers(data);

  } catch (err) {
   alert(err.message || "Failed to load users");
    alert("Failed to load users");
  } finally {
    setLoading(false);
  }
};


 const createUser = async (e) => {
  e.preventDefault();

  let idsPrediction = "BENIGN";

  // IDS scan first
  try {
    const idsRes = await fetch(
      `${IDS_BASE}/detect`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          email: formData.email,
          password: formData.password,
          role: formData.role
        })
      }
    );

    const idsData = await idsRes.json();
    idsPrediction = idsData.prediction;

  } catch (err) {
    alert("Security system unavailable. Try again later.");
    return;
  }

  if (idsPrediction !== "BENIGN") {
    alert(`Blocked by Security System: ${idsPrediction}`);
    return;
  }

  // Normal create user request
  try {
    const res = await fetch(`${API_BASE}/admin/users`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`
      },
      body: JSON.stringify(formData),
    });

    if (!res.ok) {
      const err = await res.json();
      alert(err.error || "Failed to create user");
      return;
    }

    setShowForm(false);

    setFormData({
      email: "",
      password: "",
      role: "student",
    });

    fetchUsers();

  } catch {
    alert("Failed to create user");
  }
};
  useEffect(() => {
    fetchUsers();
  }, []);
 

  // ================= RESET PASSWORD =================
  const resetPassword = async (userId) => {

    if (!window.confirm("Reset password to 123?")) return;

    try {

      const res = await fetch(
        `${API_BASE}/admin/users/${userId}/reset_password`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`
          }
        }
      );

      if (!res.ok) throw new Error();

      alert("Password reset to 123");

    } catch {
      alert("Failed to reset password");
    }
  };

  // ================= DELETE USER =================
  const deleteUser = async (userId) => {

    if (!window.confirm("Delete this user?")) return;

    try {

      const res = await fetch(
        `${API_BASE}/admin/users/${userId}`,
        {
          method: "DELETE",
          headers: {
            Authorization: `Bearer ${token}`
          }
        }
      );

      if (!res.ok) throw new Error();

      fetchUsers();

    } catch {
      alert("Failed to delete user");
    }
  };

  if (loading) return <p className="p-6">Loading users...</p>;

  return (
    <div className="p-6 bg-gray-100 min-h-screen">

      {/* ================= ADD USER BUTTON ================= */}
      <button
        onClick={() => setShowForm(true)}
        className="mb-6 px-5 py-3 bg-blue-600 hover:bg-blue-700 text-white rounded-md font-semibold"
      >
        Add New User
      </button>

      {showForm && (

        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">

          <form
            onSubmit={createUser}
            className="bg-white p-6 rounded-lg shadow-xl w-90"
          >

            <h3 className="text-lg font-semibold mb-4">
              Add New User
            </h3>
            {/* ***********************Email********************* */}

    <div className="relative w-full mb-4">
  <input
    id="email"
    type="email"
    value={formData.email}
    onChange={(e) =>
      setFormData({
        ...formData,
        email: e.target.value
      })
    }
    className="peer w-full px-3 py-3 border rounded-md focus:outline-none focus:border-indigo-500"
    placeholder=" "
    required
  />

  <label
    htmlFor="email"
    className="absolute left-3 top-3 bg-white px-1 text-gray-500 text-sm transition-all
    peer-placeholder-shown:top-3 peer-placeholder-shown:text-base
    peer-focus:-top-2 peer-focus:text-xs peer-focus:text-indigo-600"
  >
    Email
  </label>
</div>
            {/* ***********************Password********************* */}

  <div className="relative w-full mb-4">
  <input
    id="password"
    type="password"
    value={formData.password}
    onChange={(e) =>
      setFormData({
        ...formData,
        password: e.target.value
      })
    }
   className="peer w-full h-14 px-4 border border-gray-300 rounded-md bg-white appearance-none focus:outline-none focus:border-indigo-500"
    placeholder=" "
    required
  />

  <label
    htmlFor="password"
    className="
      absolute left-4 top-1/2 -translate-y-1/2
      bg-white px-1 text-gray-500 text-sm transition-all
      peer-focus:top-0 peer-focus:text-xs peer-focus:text-indigo-600
      peer-not-placeholder-shown:top-0 peer-not-placeholder-shown:text-xs
    "
  >
    Password
  </label>
</div>

          

            {/* ***********************Role********************* */}
            <select
              value={formData.role}
              onChange={(e) =>
                setFormData({
                  ...formData,
                  role: e.target.value
                })
              }
              className="w-full mb-4 px-3 py-2 border rounded-md"
            >
              <option value="student">Student</option>
              <option value="admin">Admin</option>
            </select>

            <div className="flex gap-3">

              <button
                type="submit"
                className="bg-green-500 hover:bg-green-600 text-white px-4 py-2 rounded-md"
              >
                Create
              </button>

              <button
                type="button"
                onClick={() => setShowForm(false)}
                className="bg-gray-400 hover:bg-gray-500 text-white px-4 py-2 rounded-md"
              >
                Cancel
              </button>

            </div>

          </form>

        </div>
      )}


     
   {/* ================= USERS TABLE ================= */}
<div className="bg-white rounded-xl shadow-lg overflow-hidden">

  <div className="overflow-x-auto">

    <table className="min-w-full">

      <thead className="bg-gray-100 text-left">
        <tr>
          <th className="py-3 px-4 whitespace-nowrap">Username</th>
          <th className="py-3 px-4 whitespace-nowrap">Role</th>
          <th className="py-3 px-4 whitespace-nowrap">Password</th>
          <th className="py-3 px-4 whitespace-nowrap">Actions</th>
        </tr>
      </thead>

      <tbody>
        {users.map((user) => (
          <tr
            key={user.id}
            className="border-b hover:bg-gray-50 transition"
          >
            <td className="py-3 px-4 whitespace-nowrap">
              {user.username}
            </td>

            <td className="px-4 whitespace-nowrap">
              <span
                className={`px-3 py-1 rounded-full text-xs font-semibold capitalize ${
                  user.role === "admin"
                    ? "bg-purple-100 text-purple-700"
                    : "bg-blue-100 text-blue-700"
                }`}
              >
                {user.role}
              </span>
            </td>

            <td className="px-4 whitespace-nowrap">
              ******

              <button
                onClick={() => resetPassword(user.id)}
                className="ml-3 text-blue-600 hover:underline text-sm"
              >
                Reset
              </button>
            </td>

            <td className="px-4 whitespace-nowrap">
              <button
                onClick={() => deleteUser(user.id)}
                className="bg-red-500 hover:bg-red-600 text-white px-3 py-1 rounded-md text-sm"
              >
                Delete
              </button>
            </td>
          </tr>
        ))}
      </tbody>

    </table>

  </div>
</div>

    </div>
  );
}