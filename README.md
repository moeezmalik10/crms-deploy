# CRMS – deploy package

Website (Vercel) + backend and IDS (Render, via `render.yaml`) + Supabase database + node agents on lab PCs.

Follow **DEPLOY_GUIDE.md**. After each step run `tools\CHECK_DEPLOY.bat <step>`.

| Folder | Deployed to |
|---|---|
| `backend/` | Render (crms-backend) |
| `ids/` | Render (crms-ids) |
| `frontend/` | Vercel (Root Directory = `frontend`) |
| `node-agent/` | Every PC in the pool (edit `backend_url.txt`, run `setup_agent.bat`, then `start_agent.bat`) |
| `database/` | Supabase SQL Editor (`supabase_setup.sql`, once) |
| `tools/` | Checks and test data, run on your PC |
