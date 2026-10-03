import { API_BASE } from "../../src/config";
import { useEffect, useState } from "react";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer
} from "recharts";
import { useParams } from "react-router-dom";


const getColor = (value) => {
  if (value >= 0.75) return "text-green-600";
  if (value >= 0.5) return "text-yellow-600";
  return "text-red-500";
};

export default function MLResults() {

  const { jobId } = useParams();
  const username = localStorage.getItem("username");

  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(true);

  // =========================
  // FETCH SINGLE ML STATUS
  // =========================
  async function fetchMLStatus(jobId) {
    try {
      const res = await fetch(`${API_BASE}/api/ml/status/${jobId}`);
      const data = await res.json();

      console.log("ML STATUS RESPONSE:", data);

      if (res.ok) return data;

    } catch (err) {
      console.error(err);
    }

    return null;
  }

  // =========================
  // DOWNLOAD MODEL
  // =========================
  async function downloadResult(jobId) {

    try {

      const res = await fetch(`${API_BASE}/api/ml/status/${jobId}`);
      const data = await res.json();

      if (!res.ok || !data.model_ready) {
        return alert("Model not ready");
      }

      const link = document.createElement("a");

      link.href = `${API_BASE}/api/ml/download-model/${jobId}`;
      link.download = `model_${jobId}.json`;

      document.body.appendChild(link);

      link.click();

      document.body.removeChild(link);

    } catch (err) {

      console.error(err);
      alert("Download failed");
    }
  }

  // =========================
  // FETCH RESULTS
  // =========================
  async function fetchResults() {

    try {

      // =========================
      // SINGLE JOB PAGE
      // =========================
      if (jobId) {

        const ml = await fetchMLStatus(jobId);

        if (ml) {
          setResults([{ id: jobId, raw: ml }]);
        } else {
          setResults([]);
        }
      }

      // =========================
      // ALL JOBS PAGE
      // =========================
      else {

        const res = await fetch(`${API_BASE}/tasks/my/${username}`, {
          headers: {
            Authorization: `Bearer ${localStorage.getItem("token")}`
          }
        });

        const data = await res.json();

        if (!res.ok) {
          setResults([]);
          return;
        }

        const mlJobs = (data || []).filter(
          s => s.task_type === "ml_job_parent"
        );

        const enriched = await Promise.all(

          mlJobs.map(async (job) => {

            const ml = await fetchMLStatus(job.id);

            if (ml && ml.model_ready) {
              return {
                id: job.id,
                raw: ml
              };
            }

            return null;
          })
        );

        setResults(enriched.filter(Boolean));
      }

    } catch (err) {

      console.error(err);
      setResults([]);

    } finally {

      setLoading(false);
    }
  }

  useEffect(() => {
    fetchResults();
  }, []);

  // =========================
  // LOADING
  // =========================
  if (loading) {
    return <p className="p-10">Loading ML results...</p>;
  }

  return (

    <div className="flex flex-col items-center w-full p-6">

      <h1 className="text-3xl font-bold mb-8">
        ML Task Results Dashboard
      </h1>

      {results.length === 0 ? (

        <p>No results available.</p>

      ) : (

        <div className="grid gap-8 w-full grid-cols-1 sm:grid-cols-2 lg:grid-cols-2 xl:grid-cols-3">

          {results.map((r) => {

            console.log("RAW RESULT:", r);

            // =========================
            // METRICS
            // =========================
            const metrics = r.raw?.metrics || {};

            console.log("METRICS:", metrics);

            // =========================
            // MODEL INFO
            // =========================
            const modelInfo = r.raw?.model_file_content || {};

            // =========================
            // WEIGHTS
            // =========================
            const weights =
              modelInfo?.model_parameters?.weights || [];

            // =========================
            // CHART DATA
            // =========================
            const chartData = Array.isArray(weights)
              ? weights.map((v, i) => ({
                  name: `F${i + 1}`,
                  value: Number(v) || 0
                }))
              : [];

            return (

              <div
                key={r.id}
                className="flex flex-col bg-white items-center p-4 lg:p-5 rounded-xl hover:-translate-y-1 transition duration-300 shadow"
              >

                <div className="w-full text-center">

                  {/* ========================= */}
                  {/* JOB TITLE */}
                  {/* ========================= */}

                  <h3 className="font-semibold text-lg mb-1">
                    Job #{r.id}
                  </h3>

                  {/* ========================= */}
                  {/* MODEL TYPE */}
                  {/* ========================= */}

                  <p className="text-sm mb-2">

                    Model: {

                      modelInfo?.model_metadata?.model_type ||

                      r.raw?.model_type ||

                      "N/A"
                    }

                  </p>

                  <hr className="my-2" />

                  {/* ========================= */}
                  {/* METRICS */}
                  {/* ========================= */}

                  <div className="text-sm space-y-1">

                    <p className={getColor(Number(metrics.accuracy ?? 0))}>
                      <b>Accuracy:</b>{" "}
                      {(Number(metrics.accuracy ?? 0) * 100).toFixed(2)}%
                    </p>

                    <p className={getColor(Number(metrics.precision ?? 0))}>
                      <b>Precision:</b>{" "}
                      {(Number(metrics.precision ?? 0) * 100).toFixed(2)}%
                    </p>

                    <p className={getColor(Number(metrics.recall ?? 0))}>
                      <b>Recall:</b>{" "}
                      {(Number(metrics.recall ?? 0) * 100).toFixed(2)}%
                    </p>

                    <p className={getColor(Number(metrics.f1_score ?? 0))}>
                      <b>F1 Score:</b>{" "}
                      {(Number(metrics.f1_score ?? 0) * 100).toFixed(2)}%
                    </p>

                  </div>

                  {/* ========================= */}
                  {/* FEATURE WEIGHTS CHART */}
                  {/* ========================= */}

                  <div className="mt-4">

                    <p className="font-semibold text-sm mb-2">
                      Model Weights
                    </p>

                    {chartData.length === 0 ? (

                      <p className="text-xs text-gray-500">
                        No weight data available
                      </p>

                    ) : (

                      <ResponsiveContainer width="100%" height={180}>

                        <BarChart data={chartData}>

                          <XAxis dataKey="name" />

                          <YAxis />

                          <Tooltip
                            formatter={(v) => Number(v).toFixed(4)}
                          />

                          <Bar dataKey="value" />

                        </BarChart>

                      </ResponsiveContainer>
                    )}
                  </div>

                  {/* ========================= */}
                  {/* DOWNLOAD BUTTON */}
                  {/* ========================= */}

                  <button

                    disabled={!r.raw?.model_ready}

                    onClick={() => downloadResult(r.id)}

                    className={`mt-4 w-[80%] py-2 rounded-md ${
                      r.raw?.model_ready
                        ? "bg-blue-600 hover:bg-blue-700 text-white"
                        : "bg-gray-400 text-gray-200 cursor-not-allowed"
                    }`}
                  >

                    {r.raw?.model_ready
                      ? "Download Model"
                      : "Processing..."}

                  </button>

                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}