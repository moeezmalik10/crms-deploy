import { API_BASE } from "../../src/config";
import React, { useState } from "react";
import { useNavigate } from "react-router";

export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  const navigate = useNavigate();

  const handleLogin = async (e) => {
    e.preventDefault();
    setError("");

    try {
      // The IDS screening used to happen here too, as a direct browser->IDS call before this
      // request - removed (see SESSION_SUMMARY.md): auth_routes.py::_ids_block_reason already
      // screens every login server-side (the reason that was added was precisely so screening
      // can't be skipped by calling the backend directly), so this second client-side call was
      // redundant for security and was double-counting R2's new per-IP rate/new-device features
      // (ids/ids/features.py) - every real login attempt was incrementing them twice.
      const res = await fetch(`${API_BASE}/auth/login`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          email: email,
          password: password,
          "Destination Port": 80,
          "Flow Duration": 100,
        }),
      });

      const data = await res.json();

      if (!res.ok) {
        setError(data.error || "Invalid Email Or Password");
        return;
      }

      // Save token + user info
      localStorage.setItem("token", data.access_token);
      localStorage.setItem("email", email);
      localStorage.setItem("user_id", data.user.id);
      localStorage.setItem("username", data.user.username);
      localStorage.setItem("role", data.user.role);

      // Navigate
      if (data.user.role === "admin") {
        navigate("/adminpage/machines");
      } else {
        navigate("/studentpage/newrequest");
      }
    } catch (err) {
      setError("Server not reachable");
    }
  };

  return (
    <div className="main-container">
      <div className="outer-container">
        <h1 className="text-lg font-bold tracking-wider text-[#777] md:text-3xl">
          RHL CRM
        </h1>

        <form
          onSubmit={handleLogin}
          className="flex flex-col items-center gap-5"
        >
          <div className="relative w-full">
            <input
              id="email"
              className="peer"
              type="text"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder=""
            />

            <label
              htmlFor="email"
              className="text-xs absolute left-3 top-3 -translate-y-3 md:translate-x-14 px-1 text-gray-500 peer-focus:-py-3 transition-all peer-focus:top-4 peer-focus:text-xs peer-focus:text-indigo-600 peer-placeholder-shown:top-1/2 peer-placeholder-shown:text-base "
            >
              Email
            </label>
          </div>

          <div className="relative w-[85%]">
            <input
              id="password"
              className="peer"
              type="password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder=""
            />

            <label
              htmlFor="password"
              className="text-xs
               absolute 
                top-3 
                px-1 
                -translate-y-3
                 md:translate-x-14
                  text-gray-500 
                  peer-focus:-py-3
                   transition-all
                    peer-focus:top-4 -left-7 peer-focus:text-xs peer-focus:text-indigo-600 peer-placeholder-shown:top-1/2 peer-placeholder-shown:text-base "
            >
              Password
            </label>
          </div>

          {error && <p className="text-red-500 text-sm">{error}</p>}

          <button
            type="submit"
            className="bg-[#2945e4e2] w-[80%] rounded-2xl p-3"
          >
            LOGIN
          </button>
        </form>
      </div>
    </div>
  );
}