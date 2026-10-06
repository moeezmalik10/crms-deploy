import { API_BASE, IDS_BASE } from "../../src/config";
import React, { useState } from "react";
import { useNavigate } from "react-router";


export default function NewRequests() {

  const [taskType, setTaskType] = useState("");
  const [duration, setDuration] = useState("");
  const [accessMode, setAccessMode] = useState("");
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(false);

  const navigate = useNavigate();

  const userId = Number(localStorage.getItem("user_id"));

  const handleSubmit = async (e) => {

    e.preventDefault();

    setMessage("");

    if (!userId) {
      setMessage("User not logged in");
      return;
    }

    if (!taskType || !accessMode || Number(duration) <= 0) {
      setMessage("Please fill all the required fields");
      return;
    }

    setLoading(true);

    const payload = {
      user_id: userId,
      task_type: taskType,
      mode: accessMode,
      duration_minutes: Number(duration)
    };

    let idsPrediction = "BENIGN";

    // ================= IDS CHECK =================
    try {

      const idsRes = await fetch(
        `${IDS_BASE}/detect`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json"
          },
          body: JSON.stringify({
            email: localStorage.getItem("email"),
            task_type: taskType,
            duration: duration,
            mode: accessMode
          })
        }
      );

      const idsData = await idsRes.json();

      idsPrediction = idsData.prediction;

      console.log("IDS Prediction:", idsPrediction);

    } catch (err) {

      setMessage("Security system unavailable. Try again later.");
      setLoading(false);
      return;

    }

    // ================= BLOCK MALICIOUS REQUEST =================
    if (idsPrediction !== "BENIGN") {

      setMessage(`Suspicious activity detected: ${idsPrediction}`);
      setLoading(false);
      return;

    }

    // ================= MAIN REQUEST =================
    try {

      console.log("TOKEN:", localStorage.getItem("token"));

      const res = await fetch(`${API_BASE}/tasks/request`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("token")}`
        },
        body: JSON.stringify(payload)
      });

      const data = await res.json();

      console.log("REQUEST RESPONSE:", data);

      if (!res.ok) {

        setMessage(data.error || "Request Failed");
        return;

      }

      if (data?.allocation?.error) {

        setMessage(data.allocation.error);
        return;

      }

      setMessage("Request submitted successfully");

      // Reset form
      setAccessMode("");
      setDuration("");
      setTaskType("");

      // Optional redirect
      // navigate("/studentpage/sessions")

    } catch (err) {

      console.error(err);

      setMessage("Server Error");

    } finally {

      setLoading(false);

    }

  };

  return (

    <div className="main-container w-full">

      <div className="outer-container">

        <h1 className="text-[22px] font-bold text-black md:text-3xl">
          Send Resource Request
        </h1>

        <p className="text-xs text-[#777] md:text-sm -mt-4 w-full">
          Provide task details for resource allocation
        </p>

        <div>

          {/* FORM */}
          <form
            onSubmit={handleSubmit}
            className="flex flex-col items-center gap-5"
          >

            {/* TASK TYPE */}
            <div className="relative w-full">

              <select
                className="bg-[rgb(216,216,216)] rounded-lg p-4 md:w-[85%] w-full outline-none border border-gray-400 focus:border-indigo-500"
                required
                value={taskType}
                onChange={(e) => setTaskType(e.target.value)}
              >

                <option value="" disabled>
                  Select Task Type
                </option>

                <option value="android_studio">
                  Android Studio
                </option>

                <option value="visual_studio">
                  Visual Studio
                </option>

                <option value="vs_code">
                  VS Code
                </option>

                <option value="dev_cpp">
                  Dev C++
                </option>

                <option value="ubuntu">
                  Ubuntu VM
                </option>

                <option value="anylogic">
                  AnyLogic
                </option>

                <option value="oracle">
                  Oracle DB
                </option>

                <option value="sumo">
                  SUMO
                </option>

              </select>

            </div>

            {/* DURATION */}
            <div className="relative w-full">

              <input
                id="duration"
                className="peer text focus:border-indigo-500 min-1"
                type="number"
                required
                value={duration}
                onChange={(e) => setDuration(e.target.value)}
                placeholder=""
              />

              <label
                htmlFor="duration"
                className="absolute top-3 left-3 text-gray-500 text-xs -translate-y-3 md:translate-x-14 px-1 peer-focus:-py-3 transition-all peer-focus:top-2 peer-focus:bg-[rgb(216,216,216)] peer-focus:text-sm peer-focus:text-indigo-600 peer-placeholder-shown:top-1/2 peer-placeholder-shown:text-base"
              >
                Duration (minutes)
              </label>

            </div>

            {/* ACCESS MODE */}
            <div className="flex mt-4 justify-evenly items-center w-[60%] text-sm">

              <label>

                <input
                  type="radio"
                  name="accesstype"
                  value="physical"
                  checked={accessMode === "physical"}
                  onChange={(e) => setAccessMode(e.target.value)}
                />

                {" "}Physical

              </label>

              <label>

                <input
                  type="radio"
                  name="accesstype"
                  value="remote"
                  checked={accessMode === "remote"}
                  onChange={(e) => setAccessMode(e.target.value)}
                />

                {" "}Workspace (in browser)

              </label>

            </div>

            {/* SUBMIT BUTTON */}
            <button
              type="submit"
              disabled={loading}
              className="bg-[#2945e4e2] md:mt-4 w-[90%] rounded-xl p-3 text-white font-bold shadow-xl transition-all hover:scale-105"
            >

              {loading ? "Submitting..." : "Submit Request"}

            </button>

            {/* MESSAGE */}
            {message && (
              <p className="text-sm text-center">
                {message}
              </p>
            )}

          </form>

        </div>

      </div>

    </div>

  );

}