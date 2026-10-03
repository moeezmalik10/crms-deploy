// Server addresses, set in Vercel -> Project -> Settings -> Environment Variables
// (or in frontend/.env for running on your own PC). See .env.example.
export const API_BASE = (import.meta.env.VITE_API_BASE || "http://localhost:8000").replace(/\/$/, "");
export const IDS_BASE = (import.meta.env.VITE_IDS_BASE || "http://localhost:5001").replace(/\/$/, "");
