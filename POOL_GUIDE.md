# CRMS Resource Pool – what was added and how to test it

Students lend a **slice** of their PC (CPU cores, RAM, disk) to the pool. Other students use that slice
remotely through **sandboxes**. Nobody gets another student's desktop or files.

## What is new

| Feature | Where |
|---|---|
| **Contribute Resources**: a student creates a one-time join code, sets how much to lend (cores, RAM, storage), and adds the PC. Pause, change limits or leave at any time. | Student menu → Contribute Resources |
| **Device keys**: each contributed PC gets its own secret key when it joins. Every agent request must carry it; a stolen PC name is useless without it. Removing a device cancels its key at once. | Automatic |
| **Resource Pool dashboard**: one bar per resource built from each device's share; online/paused/offline with last-seen; LAN + public IP; cores, GHz, RAM, disk; totals (cores, CPU power, RAM, storage); pause/remove. | Admin menu → Resource Pool (students see it without IPs) |
| **Run a Job**: upload Python/C++ (or a zip), choose cores/RAM/disk/time; it runs in a sandbox on a free device; download the results. | Student menu → Run a Job |
| **Workspace** (request mode "Workspace (in browser)"): VS Code in the browser in a sandbox with its own CPU/RAM/disk slice, via a private https link + password; deleted when the time ends. | New Request → Workspace; then My Workspace |
| **Pool Storage**: files are encrypted (AES-256-GCM), split into 4 MB parts and kept on **two** contributors' disks; still downloadable when one PC is off. | Student menu → Pool Storage |
| **Split a job across devices**: choose 2-8 parts; each part runs at the same time on a different device, and a CSV data file is shared out by rows. Results come back as one zip with the CSV outputs merged, plus the time saved. | Run a Job → Split across devices |
| **Offline detection**: a PC that is switched off shows **Offline** within about a minute; its unstarted requests move to another device. | Automatic |

## Security

* Agents only make **outgoing HTTPS** calls; a contributed PC opens no ports (the agent's local page now listens on 127.0.0.1 only).
* Join codes are one-time and expire after 30 minutes; only hashes of codes and device keys are stored.
* Job sandbox (Docker): no network, capped CPU/RAM/processes, read-only system, no Linux capabilities, its own folder only, deleted afterwards.
* "Light sandbox" (no Docker) only if the owner ticks it: CPU, RAM, disk and time limits, but **not isolated** from the PC's files.
* Storage parts are encrypted before they leave the server; contributors cannot read them.
* Passwords for workspaces are shown only to the logged-in owner of the session.
* `ALLOW_LEGACY_AGENTS=false` on Render makes a device key compulsory for every agent (the old lab-PC agent and browser devices then stop working).

## Deploy

1. Copy the updated files into `crms-deploy` (backend, frontend, node-agent, tools), then:
   ```
   git add .
   git commit -m "Resource pool: contribute, jobs, workspaces, storage"
   git push
   ```
2. Wait until Render and Vercel are ready. The backend adds the new database columns and tables by itself.
3. Do **not** change `JWT_SECRET_KEY` on Render after files are stored in the pool (it also protects the storage keys).
   To keep them independent, add an environment variable `POOL_MASTER_KEY` (any long random text) **before** storing files.

## Set up a contributor PC (e.g. MOEEZ)

1. Install **Docker Desktop** (free for education) and start it once. Without Docker the PC can still do Physical sessions and,
   if the owner allows, light-sandbox jobs.
2. Log in as the student → **Contribute Resources** → set the limits → **Create join code**.
3. Either download the agent from that page, or update your existing `cloud-agent` folder with the new
   `agent.py`, `pool_runtime.py` and `start_agent.bat`. Run `setup_agent.bat` once if it is a new folder.
4. Run `start_agent.bat`, paste the server address and the join code. The window shows
   `Device key : yes - verified pool device` and `Sandbox : docker`.
5. The first time, the agent downloads the sandbox images (several hundred MB); this runs in the background.

## Test checklist

| Test | Expected |
|---|---|
| `CHECK_DEPLOY.bat pool` | Devices online with LAN/public IP and sandbox type; pool totals |
| Admin → Resource Pool | Bars for CPU, memory, storage; device rows; totals |
| Close the agent window (or switch the PC off) | Device turns **Offline** within ~1 minute with "last seen" |
| Run a Job: `main.py` that writes `output/result.txt` | Status Finished, output shown, **Download results** gives a zip with `result.txt` |
| Job that uses more memory than requested | Failed: "memory limit of … MB exceeded" |
| Job that tries the internet | Prints that the network is blocked |
| Pool Storage: upload a file with two contributor PCs online | "2 copies on devices", kept on both PC names |
| Switch one of the two PCs off, Prepare download, Download | File downloads unchanged |
| Request **Workspace (in browser)**, open My Workspace | https link + password; VS Code opens; deleted when time ends or session is deleted |
| Pause sharing on a device | No new work is placed on it |

## Combining devices for one job (split jobs)

A single running program cannot use the RAM or CPU of two PCs at once: memory over the network is
thousands of times slower than local memory, so "merging" two PCs into one big computer would make it slower,
not faster. The pool combines devices the way real clusters do: the work is **divided** and every device runs its
own part **at the same time**.

* Run a Job → **Split across devices** = 2-8 parts. Cores, memory and disk are **per part**.
* Optional **data file** (CSV/TXT): rows are shared out, each part gets its own slice (the header row is kept in every part).
* Inside the program, `CRMS_PART` and `CRMS_PARTS` say which part it is (e.g. 2 of 3).
* When all parts finish, **Download merged results** gives `part_1/`, `part_2/`, ..., `merged/<name>.csv`
  (CSV outputs joined into one) and `summary.txt`.
* The page shows where each part ran, how long it took, and how much faster the split was.

What keeps it fast:

* Parts are placed on **different devices** first; a device is only given a second part if it has spare cores.
* When a part finishes, the next waiting part is placed at once.
* Pool devices check for work every 5 seconds (lab PCs every 15).
* Each part needs a few seconds to start, so split jobs that take more than about a minute.
  Local test: 24-row prime count took 33 s on one device and 18 s split in 2 parts (1.8x faster).

Example program (`main.py`, with a data file `numbers.csv` that has a column `n`):

```python
import csv, os
rows = list(csv.DictReader(open("numbers.csv")))        # only this part's rows
print("part", os.environ["CRMS_PART"], "of", os.environ["CRMS_PARTS"], "-", len(rows), "rows")
with open("output/result.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow(["n", "square"])
    for r in rows:
        w.writerow([r["n"], int(r["n"]) ** 2])
```
