# CRMS – Deployment Guide (step by step, with a check after every step)

This package is your project, ready to deploy:
- the **website** goes to **Vercel**;
- the **backend** and the **IDS** go to **Render** (both created at once from `render.yaml`);
- the **database** stays on your existing **Supabase** project;
- every PC in the pool runs the **node agent** and connects over the internet, so **no Tailscale is needed**.

```
 Phone / laptop browser ──► Website (Vercel)
                               │              │
                               ▼              ▼
                    Backend (Render)     IDS (Render)
                               │              │
                               ▼              ▼
                        Supabase (database + dataset storage)
                               ▲
      Node agents: your PC + a second real PC (heartbeats, sessions, ML jobs)
```

**Three rules before you start:**
1. **Don't run your local copy (`START_CRMS.bat`) while the cloud version is running.** Both use the same database and would fight over the same tasks. Run `STOP_CRMS.bat` first.
2. **Free Render services sleep** after 15 minutes without traffic; the first visit after that takes about a minute. All free services share **750 hours a month**, so close the agents when you're not testing.
3. **No passwords or keys are in this code.** You type them once into the Render and Vercel dashboards. Still make the GitHub repo **Private**.

**How checking works:** open Command Prompt in `D:\CRMS\crms-deploy\tools` and run `CHECK_DEPLOY.bat <step>`, for example `CHECK_DEPLOY.bat backend`. Move on only when it ends with **PASSED**.

---

## The values you'll need

All of them are in your `crms_config.ps1` from the run kit:

| Value | From `crms_config.ps1` | Used by |
|---|---|---|
| Database address | `DatabaseUrl` | Render backend `DATABASE_URL` |
| Login secret | `JwtSecret` | Render backend `JWT_SECRET_KEY` |
| Supabase address | `SupabaseUrl` | Render backend + IDS `SUPABASE_URL`, Vercel `VITE_SUPABASE_URL` |
| **service_role** key | `ServiceRoleKey` | Render backend + IDS `SUPABASE_KEY` (**never** Vercel) |
| **anon** key | `AnonKey` | Vercel `VITE_SUPABASE_ANON_KEY` only |

---

## Step 0 – Accounts and tools (once)

| Need | Where | Notes |
|---|---|---|
| GitHub account | github.com | Free |
| Git for Windows | git-scm.com | Install with the defaults |
| Render account | render.com → **Sign in with GitHub** | Free services, no card |
| Vercel account | vercel.com → **Continue with GitHub** | Free Hobby plan |
| Second Windows PC | A group member's laptop or a lab PC | Windows 10/11, 8 GB RAM or more, any internet |
| Python 3.12 | python.org, on **both** PCs | Tick **Add python.exe to PATH** |

✅ **Check:** in Command Prompt, `git --version` prints a version number.

---

## Step 1 – Unpack the package

Extract `CRMS_Deploy_Package.zip` to `D:\CRMS\`. You get `D:\CRMS\crms-deploy\`:

```
crms-deploy\
  render.yaml          <- creates both Render services
  backend\             <- Flask API             (Render)
  ids\                 <- intrusion detection   (Render)
  frontend\            <- React website         (Vercel)
  node-agent\          <- copy to every PC in the pool
  database\supabase_setup.sql
  tools\CHECK_DEPLOY.bat, deploy_config.ps1, sample_ml_dataset.csv
  DEPLOY_GUIDE.md      <- this guide
```

✅ **Check:** `CHECK_DEPLOY.bat files` → **PASSED**. It also confirms that no key is written in the code.

---

## Step 2 – Put the code on GitHub (private)

1. On github.com, click **New repository**. Name it `crms-deploy`, choose **Private**, don't add a README, and click **Create repository**.
2. In Command Prompt:
   ```
   cd /d D:\CRMS\crms-deploy
   git init
   git add .
   git commit -m "CRMS deploy"
   git branch -M main
   git remote add origin https://github.com/<your-username>/crms-deploy.git
   git push -u origin main
   ```
   On the first push a browser window asks you to sign in to GitHub. Allow it.

✅ **Check:**
- `CHECK_DEPLOY.bat github` → **PASSED**.
- On github.com the repo shows **Private** and the folders `backend`, `database`, `frontend`, `ids`, `node-agent` and `tools`.

---

## Step 3 – Backend and IDS on Render (one Blueprint)

1. On render.com, click **New → Blueprint**, connect GitHub if asked, and select **crms-deploy**.
2. Render reads `render.yaml` and shows two services, **crms-backend** and **crms-ids**, and asks for these values:

   | Service | Key | Value |
   |---|---|---|
   | crms-backend | `DATABASE_URL` | your database address |
   | crms-backend | `JWT_SECRET_KEY` | your login secret |
   | crms-backend | `SUPABASE_URL` | your Supabase address |
   | crms-backend | `SUPABASE_KEY` | your **service_role** key |
   | crms-backend | `CORS_ALLOWED_ORIGINS` | `http://localhost:5173` for now (changed in step 5) |
   | crms-ids | `SUPABASE_URL` | your Supabase address |
   | crms-ids | `SUPABASE_KEY` | your **service_role** key |

3. Click **Deploy Blueprint**. Wait until both services show **Live** (5–10 minutes the first time).
4. Open each service and copy its address from the top of its page. Paste them into **`tools\deploy_config.ps1`**: `BackendUrl` for the crms-backend address, `IdsUrl` for the crms-ids address. Save the file.

   If a name was taken, Render adds a few letters to the address (e.g. `crms-backend-ab12.onrender.com`). Always copy the real address.

✅ **Check:**
- In each service's **Logs** tab, the build shows `Using Python version 3.12…`.
- `CHECK_DEPLOY.bat backend` → **PASSED**. It opens `/health` and logs in as admin, which proves Render can reach your database.
- `CHECK_DEPLOY.bat ids` → **PASSED**.

| If it fails | Fix |
|---|---|
| Build log shows Python 3.14, or errors building numpy/psycopg2 | On that service, open **Environment**, check that `PYTHON_VERSION` is `3.12.11`, then **Manual Deploy → Deploy latest commit** |
| Backend: `could not translate host name` / `Tenant or user not found` | `DATABASE_URL` is wrong. Copy it again |
| Backend `/health` ok, but admin login fails | Run `database\supabase_setup.sql` in Supabase → SQL Editor (it resets the admin password) |
| IDS: `/blacklisted` fails or says "Invalid API key" | `SUPABASE_URL` or `SUPABASE_KEY` is wrong on crms-ids. It must be the **service_role** key |
| IDS log: `WARNING: IDS model not available` | The model couldn't be downloaded from Google Drive. The IDS still runs with its SQL-injection and XSS rules, and the check still passes. To use the AI model, put `ids_multiclass_model.pkl` in `ids\ids\`, then commit and push |

---

## Step 4 – Website on Vercel

1. On vercel.com, click **Add New → Project**, then **Import** `crms-deploy`.
2. Set **Root Directory** to `frontend`. The Framework Preset shows **Vite** by itself.
3. Open **Environment Variables** and add four:

   | Name | Value |
   |---|---|
   | `VITE_API_BASE` | your `BackendUrl` |
   | `VITE_IDS_BASE` | your `IdsUrl` |
   | `VITE_SUPABASE_URL` | your Supabase address |
   | `VITE_SUPABASE_ANON_KEY` | your **anon** key (not service_role) |

4. Click **Deploy** and wait about a minute.
5. Copy the main address (e.g. `https://crms-deploy.vercel.app`) into `FrontendUrl` in `tools\deploy_config.ps1`.

✅ **Check:** `CHECK_DEPLOY.bat website` → **PASSED**. It checks that the page loads, that inner pages refresh, and that the website calls your Render backend and IDS.

**If you change a Vercel variable later,** open **Deployments → ⋯ → Redeploy**. The website only picks up new values when it's rebuilt.

---

## Step 5 – Let the backend accept the website

1. In Render, open **crms-backend → Environment**. Set `CORS_ALLOWED_ORIGINS` to your Vercel address, exactly as copied, with no `/` at the end.
2. Click **Save, rebuild and deploy** and wait for **Live**.

✅ **Check:**
- `CHECK_DEPLOY.bat cors` → **PASSED**.
- Open the Vercel address and log in with `admin@uog.edu.pk` / `Admin@12345`. The **Machines** page opens.
- Try `test' OR 1=1 --` as the email. You should see *Suspicious activity detected: SQL Injection Attempt*, and the attempt appears on the **IDS dashboard**. After 3 tries your IP gets blocked; unblock it on **Blocked IP** or in Supabase → Table Editor → `blacklisted_ips`.

---

## Step 6 – Your PC joins the pool

1. Run `STOP_CRMS.bat` so nothing local is running.
2. Copy `D:\CRMS\crms-deploy\node-agent` to `D:\CRMS\cloud-agent`.
3. Open `backend_url.txt` in that folder, replace the text with your **BackendUrl**, and save.
4. Double-click **`setup_agent.bat`** (first time only), then **`start_agent.bat`**. The window shows your backend address, then `Heartbeat sent` every 30 seconds.

✅ **Check:**
- `CHECK_DEPLOY.bat nodes` → your PC (e.g. **MOEEZ**) is **online**.
- The website's **Machines** page shows it with live CPU and RAM.

---

## Step 7 – Second PC joins the pool

1. Run `hostname` on both PCs. **The names must be different**, or the backend sees one PC. Rename one if needed: **Settings → System → About → Rename this PC**, then restart.
2. Copy your `D:\CRMS\cloud-agent` folder, with `backend_url.txt` already filled in, to the second PC. Use a USB drive, or zip it and send it. It contains no passwords.
3. On the second PC, run **`setup_agent.bat`**, then **`start_agent.bat`**.
4. While testing, stop it sleeping: **Settings → System → Power → Never**.

✅ **Check:** `CHECK_DEPLOY.bat nodes` → **2 PC(s) online**, and both appear on **Machines**.

| If it fails | Fix |
|---|---|
| Only one PC listed | Same computer name (point 1), or the agent window is closed |
| Shows **offline** | Its agent window shows `Heartbeat failed`: no internet, or Render is waking up (wait a minute) |
| `setup_agent.bat` fails | Install Python 3.12 with **Add to PATH** ticked |

---

## Step 8 – Pooling test: two students, two PCs

The backend gives each PC only **one physical session at a time**, so two requests must be spread across the pool.

1. Log in as admin → **Manage Users** → add `student2@uog.edu.pk` with a password, role **student**.
2. **Phone:** open the Vercel address, log in as `student1@uog.edu.pk` / `Student@12345` → menu (☰) → **New Request** → **Dev C++**, **Physical**, **15** minutes.
3. **Another browser:** log in as `student2@uog.edu.pk` and make the same request.
4. Admin → **History**.

✅ **Check:**
- Both requests show **running** within about 30 seconds.
- In **History**, the machine column shows **two different PCs**.
- After 15 minutes both show **completed**, and the PCs are idle again.

If a PC doesn't have room, the request **waits in the queue** and starts by itself as soon as a PC has room; the backend retries every 15 seconds. Dev C++ needs about 3 GB of free RAM and 1 free core on one PC.

---

## Step 9 – Pooled batch job across both PCs

1. As a student → **ML task Request** → upload `tools\sample_ml_dataset.csv` (its last column is the label) → **Logistic Regression**, **IID** split → submit.
2. Open **ML task Results**.

✅ **Check:**
- `2/2 nodes finished`: the dataset was split between the two PCs.
- Accuracy is about 85%, and **Download Model** works.

**If the upload fails,** open Supabase → **Storage** and make sure a bucket called `datasets` exists. `supabase_setup.sql` creates it.

---

## Every day after setup

| What | Do |
|---|---|
| Start testing | Open the Vercel address and wait up to a minute if Render was asleep. Run `start_agent.bat` on both PCs |
| Stop testing | Close both agent windows, so Render can sleep and save free hours |
| Health check | `tools\CHECK_DEPLOY.bat all` |
| After changing code | In `crms-deploy`: `git add .`, `git commit -m "update"`, `git push`. Render and Vercel redeploy by themselves |
| Supabase paused (after a week unused) | Supabase dashboard → **Restore** |

## What's different from the original code

| Part | Change | Why |
|---|---|---|
| backend | `psycopg2-binary>=2.9.10`, `SQLAlchemy<2.1`; Python 3.12; one gunicorn worker; old `runtime.txt` removed | The same install fixes you needed on your PC, and the scheduler runs once |
| backend | One line in `ml_orchestrator.py`: `db.session.flush()` before logging each ML chunk | Bug fix: without it, ML jobs crash on a newly created database |
| ids | Supabase address and key read from Render settings instead of the code; Gmail login removed (optional `SMTP_EMAIL` and `SMTP_APP_PASSWORD` settings); runs without the model if it can't be downloaded; `/health` route; `requirements.txt` re-saved as UTF-8 | No keys in GitHub, no use of the old group member's email, and the service can start on a small free server |
| frontend | Server addresses and the Supabase anon key read from Vercel settings (`src/config.js`, `src/supabaseClient.js`) | No hard-coded old servers or keys |
| node-agent | Backend address read from `backend_url.txt`; `setup_agent.bat`, `start_agent.bat`, `requirements.txt` | Point any PC at the backend without editing code |
| root | `render.yaml`, `.python-version`, `.gitignore`, `tools\`, `database\` | One-click Render set-up, Python 3.12, nothing unwanted uploaded, and the checks |

## Keep it safe

- **Keep the GitHub repo private.**
- **Don't post the Render address publicly.** The backend's agent routes have no login yet (Module 0 of your proposal fixes this).
- **Change the admin password** after the first login (admin **Profile** page).
