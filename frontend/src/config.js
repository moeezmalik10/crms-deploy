// Server addresses, set in Vercel -> Project -> Settings -> Environment Variables
// (or in frontend/.env for running on your own PC). See .env.example.
export const API_BASE = (import.meta.env.VITE_API_BASE || "http://localhost:8000").replace(/\/$/, "");
export const IDS_BASE = (import.meta.env.VITE_IDS_BASE || "http://localhost:5001").replace(/\/$/, "");
// R4's Guacamole gateway (see guacamole/README.md) - left unset until a real instance is
// deployed (the only one that exists today is an untested local prototype). Empty/undefined
// means "not deployed yet" to the UI, not an error - see StudentSessionLink.jsx.
export const GUACAMOLE_URL = (import.meta.env.VITE_GUACAMOLE_URL || "").replace(/\/$/, "");
