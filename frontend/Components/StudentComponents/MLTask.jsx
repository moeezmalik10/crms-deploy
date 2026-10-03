import { API_BASE } from "../../src/config";
import React, { useState } from "react";


export default function MLTask() {
  const [file, setFile] = useState(null);
  const [modelType, setModelType] = useState("");
  const [validationType, setValidationType] = useState("");

  const [fileError, setFileError] = useState("");
  const [formError, setFormError] = useState("");
  const [successMsg, setSuccessMsg] = useState("");

  const [loading, setLoading] = useState(false);

  const userId = Number(localStorage.getItem("user_id"));

  // ---------------- FILE HANDLER ----------------
  const handleFileChange = (e) => {
    const selected = e.target.files[0];

    if (!selected) return;

    // CSV validation
    if (!selected.name.toLowerCase().endsWith(".csv")) {
      setFileError("Only CSV files are allowed");
      setFile(null);
      return;
    }

    setFile(selected);
    setFileError("");
  };

  // ---------------- SUBMIT ----------------
  const handleSubmit = async (e) => {
    e.preventDefault();

    setFormError("");
    setFileError("");
    setSuccessMsg("");

    if (!userId) {
      setFormError("User not logged in");
      return;
    }

    if (!file) {
      setFileError("Please upload a CSV file");
      return;
    }

    if (!modelType || !validationType) {
      setFormError("Please select model and validation type");
      return;
    }

    setLoading(true);

    try {
      const formData = new FormData();
      formData.append("user_id", userId);
      formData.append("file", file);
      formData.append("model_type", modelType);
      formData.append("validation_type", validationType);

      const res = await fetch(`${API_BASE}/api/ml/start-training`, {
        method: "POST",
        body: formData,
      });

      const data = await res.json();

      if (!res.ok) {
        setFormError(data.error || "Request failed");
        return;
      }

      setSuccessMsg(`✅ Job Started! ID: ${data.job_id}`);

      // Reset form
      setFile(null);
      setModelType("");
      setValidationType("");
      document.getElementById("fileInput").value = "";

    } catch (err) {
      setFormError("Server Error");
    } finally {
      setLoading(false);
    }
  };

  // ---------------- UI ----------------
  return (
    <div className="main-container w-full">
      <div className="outer-container">

        {/* Heading */}
        <h1 className="text-[22px] font-bold text-black md:text-3xl">
          ML Training Task
        </h1>

        <p className="text-xs text-[#777] md:text-sm -mt-4 w-full">
          Upload dataset and select model for distributed training
        </p>

        {/* FORM */}
        <form
          onSubmit={handleSubmit}
          className="flex flex-col items-center gap-5"
        >

          {/* FILE INPUT */}
          <div className="relative w-full">
            <input
              id="fileInput"
              type="file"
              accept=".csv"
              onChange={handleFileChange}
              className={`bg-[rgb(216,216,216)] rounded-lg p-4 md:w-[85%] w-full outline-none border ${
                fileError ? "border-red-500" : "border-gray-400"
              } focus:border-indigo-500`}
            />

            {/* 🔴 FILE ERROR UNDER INPUT */}
            {fileError && (
              <p className="text-red-500 text-xs mt-1 md:w-[85%]">
                {fileError}
              </p>
            )}
          </div>

          {/* MODEL TYPE */}
          <div className="relative w-full">
            <select
              required
              value={modelType}
              onChange={(e) => setModelType(e.target.value)}
              className="bg-[rgb(216,216,216)] rounded-lg p-4 md:w-[85%] w-full outline-none border border-gray-400 focus:border-indigo-500"
            >
              <option value="" disabled>
                Select Model Type
              </option>
              <option value="logistic_regression">Logistic Regression</option>
              <option value="linear_regression">Linear Regression</option>
            </select>
          </div>

          {/* VALIDATION TYPE */}
          <div className="relative w-full">
            <select
              required
              value={validationType}
              onChange={(e) => setValidationType(e.target.value)}
              className="bg-[rgb(216,216,216)] rounded-lg p-4 md:w-[85%] w-full outline-none border border-gray-400 focus:border-indigo-500"
            >
              <option value="" disabled>
                Select Validation Type
              </option>
              <option value="none">None</option>
              <option value="kfold">K-Fold</option>
              <option value="train_test_split">Train/Test Split</option>
            </select>
          </div>

          {/* BUTTON */}
          <button
            type="submit"
            disabled={loading}
            className="bg-[#2945e4e2] md:mt-4 w-[90%] rounded-xl p-3 text-white font-bold shadow-xl transition-all hover:scale-105"
          >
            {loading ? "Processing..." : "Start Training"}
          </button>

          {/* 🔴 FORM ERROR */}
          {formError && (
            <p className="text-red-500 text-sm text-center">
              {formError}
            </p>
          )}

          {/* 🟢 SUCCESS MESSAGE */}
          {successMsg && (
            <p className="text-green-600 text-sm text-center">
              {successMsg}
            </p>
          )}

        </form>
      </div>
    </div>
  );
}