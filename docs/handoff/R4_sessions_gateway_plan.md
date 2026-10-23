# R4 handoff: VM slot pool, remote-access gateway, and pool consolidation

**For:** Maryam Tanveer
**Written by:** an AI assistant working with Muhammad Moeez on his own R3 scope, at his request,
as a starting point for you — not a substitute for your own work. Everything here is a plan to
review, adjust, and implement yourself; nothing in this file has been coded for you.

**Why consolidation is in this plan too:** the updated proposal's team-area table (Table 11)
credits pool consolidation to R4, but it was actually built under R3 earlier in this project
(with explicit approval at the time, before the proposal was rewritten).
https://github.com/moeezmalik10/crms-deploy/issues/1 was opened to raise this with the team and
is now **resolved and closed** (Oct 19, 2026) - shared credit between R3 and R4, code unchanged.
A follow-up comment (Oct 20, 2026) notes the proposal was further updated since to list Muhammad
Moeez as sole author across R1-R4, which doesn't reopen the question so much as make it moot -
there's no longer a separate teammate whose graded work is at stake. You should still understand
and be able to maintain/extend this code if it's presented as part of your component — this
plan's first section is a guided tour of what's already there, not a to-do.

---

## Part 1: Pool consolidation (already built — read before deciding what, if anything, to change)

### What the proposal asks for (part of Component 6)
> "When a large session (for example 8 GB) cannot fit on any single PC but the pool has room
> overall, the manager moves batch units away from one PC to free it."

### What exists
`backend/app/allocation_engine.py::_consolidate` — called from `allocate_task` only after the
normal placement pass already failed to find room. Key behaviors, each with direct test coverage
in `backend/tests/test_r3_consolidation.py`:
- Only ever evicts **job-mode** (divisible) work, never another session — a physical/remote
  session is someone else's non-divisible work and is never touched.
- A **physical** session needs the *entire* device (Rule A): consolidation evicts every job on a
  candidate node, not just enough to free up the numeric amount asked for, since physical
  exclusivity isn't a capacity question.
- A **remote** session only needs its own modest profile, so consolidation evicts just enough
  jobs (biggest first, fewest moves) to fit it.
- Evicted jobs are cleanly requeued (`status = "pending"`, node unassigned) — they resume from
  scratch elsewhere, which is safe because job-mode work is stateless (see R1's handoff doc for
  the related job-recovery fix from this session).
- The **Unified Quota Ledger** (`Node.used_cores`/`reserved_cores`/`used_ram_mb`/
  `reserved_ram_mb`, added this session) correctly books a physical session's **full device
  capacity**, not just its modest profile — this matters for consolidation specifically, since
  without it `/pool/ledger` would misreport free capacity on a node a physical session already
  occupies.

If you take this over, the main open question worth resolving with R3/the team is in the GitHub
issue linked above — not a technical gap, a credit/ownership one.

## Part 2: VM slot pool and one-time credentials

### What the proposal asks for (Component 6)
> "Each lab PC offers a number of virtual machine slots created from one approved image. A
> session receives a free slot and a one-time username and password that expire with the
> session."

### Current state (verify before assuming either way)
`backend/app/agent_routes.py::task_ready` stores `vm_username`/`vm_password` on the `TaskRequest`
from whatever the **agent** reports when a session starts
(`node-agent/agent.py::start_vm_for_task`). So some credential mechanism already exists end to
end — what's worth verifying rather than assuming:
- Are the credentials genuinely **unique per session**, or could the same VM image/account get
  reused across sessions on the same PC? Read `start_vm_for_task` and whatever creates the actual
  Hyper-V VM/account to confirm.
- Do credentials actually **expire with the session** (revoked/rotated when the session ends or
  is cancelled), or do they just stop being shown in the UI while still technically valid on the
  PC? This is the gap most likely to actually need work.
- "A number of virtual machine slots" (plural, pooled) — confirm whether a single PC can host
  multiple concurrent VM sessions today, or only one at a time; check `TASK_PROFILES` and the
  scoring logic in `allocation_engine.py::allocate_task` for how much of a device's capacity one
  physical/remote session is assumed to consume.

## Part 3: Secure remote-access gateway (the largest piece)

### What the proposal asks for (Component 11)
> "Apache Guacamole, running on the campus server, checks the token and relays the remote desktop
> to the browser, so no client software is needed and no port on any lab PC is exposed to the
> internet... No inbound ports on lab PCs. The node agent opens an outbound WebSocket connection
> to the gateway... Token bound to the session... Idle detection."

Success criteria (Objective 7 / M3): session opens from off-campus within 60s; no inbound ports
on lab PCs; access tokens expire with the session; every remote session logged.

### What exists today instead
Remote (`mode="remote"`) workspace sessions currently use **Cloudflare Quick Tunnels**, not
Guacamole: `node-agent/pool_runtime.py::_open_tunnel`/`_cloudflared` downloads `cloudflared` and
opens a free `trycloudflare.com` HTTPS tunnel to the container's local port. This already
satisfies "no inbound ports on lab PCs" (it's an outbound tunnel) and "no client software," but
it is **not** the proposal's architecture — no Guacamole, no campus-server-issued token, no
explicit idle-detection/logging layer, and a `trycloudflare.com` URL isn't the kind of access
control a signed, session-bound token is.

Whether to replace this or build Guacamole alongside it is a real design decision, not just
"swap one thing for another" — see the dependency note below before committing to a plan.

### The real dependency: this needs the campus server
Guacamole, as specified, runs on the Department's self-hosted campus server (Docker, Nginx,
Postgres) — the proposal's target infrastructure, which the project **has not migrated to**
(still Render/Vercel/Supabase). This is flagged as a whole-team decision in `SESSION_SUMMARY.md`'s
known-gaps list, not something to start solo. Two honest paths:
1. **Prototype first, on a dev box or the campus server once it exists**: stand up Guacamole in
   Docker, get a single hardcoded RDP connection working through it, confirm the token-gating
   model end to end, before touching production routing.
2. **Design the token contract now, independent of where Guacamole eventually runs**: the portal
   needs to issue a short-lived, signed token naming one student and one VM slot
   (`backend/app/student_routes.py`/`agent_routes.py` are the natural home for this, following
   the existing `flask_jwt_extended` pattern already used for login tokens). This part doesn't
   need the campus server to design or even partially build — only the final Guacamole relay does.

### Concrete steps, in roughly dependency order
1. **Token issuance**: when a session starts, the portal issues a signed token (student ID + VM
   slot + expiry) — reuse `flask_jwt_extended`, don't invent a new signing scheme.
2. **Token validation + idle detection**: whatever relays the session (Guacamole or, as an interim
   step, the current Cloudflare-tunnel path) must check the token is valid, not expired, and not
   reused, and must track activity to close idle connections — none of this exists today.
3. **Access logging**: "every remote session logged" — check whether `TaskExecutionLog` already
   captures connect/disconnect events for remote sessions, or whether that needs a dedicated log
   table/view for the gateway specifically.
4. **The actual Guacamole relay** — the piece that genuinely needs the campus server, Docker, and
   Nginx with WSS. Don't start this until the infrastructure decision is made at the team level.

## Suggested order
1. Verify Part 2's open questions (cheap — mostly reading, not building) and close any real gap
   found (likely credential expiry-on-session-end).
2. Design and build the token contract (Part 3, step 1–2) — doesn't block on infrastructure.
3. Raise the campus-server migration question with the team explicitly, since Part 3's final step
   depends on it and nobody's decided yet.
4. Take ownership of `_consolidate` (Part 1) whenever the GitHub issue resolves — read the tests
   first, they document the exact behavior better than prose can.

## Testing
`backend/tests/test_r3_consolidation.py` is the existing pattern to follow for Part 1. Parts 2
and 3 have no test coverage yet; the token issuance/validation logic in particular should be
straightforwardly testable the same way the rest of this project's JWT-based auth is (see
`backend/tests/test_r3_quotas.py`'s `auth` fixture for the pattern).

## 2g. R4 started: Part 2 verified, the token contract built, a local prototype scaffolded (not run)

**Part 2, verified by reading rather than assumed:**
- The code path this plan's "unique per session?"/"expires with session?" questions were really
  about is `node-agent/pool_runtime.py::start_workspace` (the actual, currently-used `remote`-mode
  path) - `node-agent/agent.py`'s Hyper-V/`VM_CONFIGS` path with hardcoded `student`/`123` is
  disabled by default (`USE_HYPERV_VMS`, off unless a node operator manually drops a sentinel
  file) and is labeled "older VM sessions" in `StudentSessionLink.jsx` - not what students
  actually use today.
- On the real path: `_password()` generates a genuinely random 12-character password **per
  session** (`start_workspace`'s own `pw = _password()`), and the whole container - the only
  place that password is valid - is destroyed on teardown (`stop_workspace`'s `docker rm -f`).
  So "unique per session" and "expires with session" were **already true** for the path that
  matters, just not via a Guacamole/token architecture. The one genuine, still-open gap found:
  "physical" mode (`mode=="physical"` in `agent.py`) sends a single hardcoded
  `HOST_USERNAME`/`HOST_PASSWORD` ("lab_user"/"lab_123") for every session on every PC, forever -
  not fixed here, since physical mode means in-person access at the lab (Component 11 is
  specifically about *off-campus*/remote access), so it's a lower-priority, separate concern from
  this plan's actual scope. Flagging it here rather than silently leaving it unmentioned.
- "A number of VM slots" - the real path already pools correctly: `_workspaces` is keyed by
  `task_id`, not a fixed VM name, so a PC can host multiple concurrent workspace containers
  limited only by its sliced capacity, not a fixed VM count.

**Part 3 steps 1-2, built as real token issuance** (not the token-validation architecture
originally sketched above - see the note below on why that changed):
- `backend/app/guacamole_gateway.py` builds Guacamole's own `guacamole-auth-json` token format -
  HMAC-SHA256 sign, AES-128-CBC encrypt (zero IV, PKCS7 padding), base64 - verified byte-for-byte
  against Apache's own reference script
  (`extensions/guacamole-auth-json/doc/encrypt-json.sh`) via real `openssl` subprocess calls in
  `backend/tests/test_r4_guacamole_gateway.py`, not just against another Python implementation of
  the same potential misunderstanding. This is **not** a `flask_jwt_extended` JWT - Guacamole's
  JSON-auth extension expects this exact, non-negotiable format, so the earlier "reuse
  flask_jwt_extended" idea above doesn't apply to this specific external contract (it still
  applies to this app's own internal tokens, unaffected).
- `issue_ssh_connection_token(task, requesting_user)` grants exactly one SSH connection into the
  *requesting student's own, currently running* session - never another student's, never a
  session that isn't running, never past its own expiry (SQLite's own tzinfo-dropping round-trip
  handled the same way `app.allocation_engine` already does elsewhere). `GET
  /tasks/<task_id>/guacamole-token` (`backend/app/student_routes.py`) exposes it, reusing the
  existing `_owns_task` ownership pattern.
- New `TaskRequest.ssh_port` column (migration in `app/migrations.py` + `database/
  supabase_setup.sql`, same idempotent `ADD COLUMN IF NOT EXISTS` pattern as every other column
  here), populated by `agent_routes.py::task_ready` from the node agent's own ready payload.
- **Why SSH, not RDP/VNC**: Guacamole relays RDP/VNC/SSH, not arbitrary HTTP apps, and the actual
  remote path today is code-server (VS Code over HTTP via the existing Cloudflare Quick Tunnel) -
  that can't be fronted by Guacamole directly. `node-agent/pool_runtime.py::WORKSPACE_DOCKERFILE`
  now also installs `openssh-server` into the workspace image (reusing the SAME per-session
  password, not a second secret) via the LinuxServer.io base image's own documented
  `/custom-cont-init.d` and `/custom-services.d` hooks - this was a deliberate choice among three
  options (SSH into the workspace container / add a VNC desktop / resurrect the disabled Hyper-V
  RDP path), not an obvious default.

**What's still genuinely untested**: this dev machine has no Docker installed. The token crypto
is verified at the byte level (see above); none of the following is: that the workspace image
actually builds with sshd working, that a real Guacamole instance actually accepts and relays a
token-gated SSH connection, or that any of this holds up under real use. `guacamole/` has a local
prototype stack (`docker-compose.yml` + `README.md`) written against Guacamole's own documentation
- explicitly **not run here** - for whoever has Docker to actually verify before trusting this
further.

**Still open, unchanged from Part 3's original scope**: the actual outbound-tunnel problem for a
lab PC with no inbound ports (this prototype assumes `guacd` and the workspace container are on
the same machine - true for a dev-box prototype, not true for a real lab PC); idle detection;
connect/disconnect access logging (Guacamole's own connection history needs a real JDBC user
database, not just JSON auth alone - not yet checked whether that's needed or how it'd combine).
The campus-server migration question (whether this ever runs anywhere but a dev box) is still
nobody's explicit decision - unchanged from this plan's original framing.

### 2i. Frontend: a "Connect via SSH" option, verified live

`StudentSessionLink.jsx` now has an "SSH access (experimental)" section for a running workspace
session - a button that calls `GET /tasks/<id>/guacamole-token` on demand (not on every 5s poll;
a token is only good for the one session, no point minting one nobody's about to use), then
either an error (the backend's own message - "no SSH target yet", "not running", etc. - with a
"Try again" button) or a "Connect via SSH" link to
`${VITE_GUACAMOLE_URL}/#/?data=<url-encoded token>`, Guacamole's own documented way to open a
session from a query parameter.

The whole section only renders when `VITE_GUACAMOLE_URL` (new, `frontend/src/config.js` +
`.env.example`) is set - empty/unset, the default, means "no gateway deployed yet" and the
section doesn't appear at all, rather than showing students a button that can't work. Anyone
running the local prototype (`guacamole/`) end to end sets it in their own `frontend/.env`.

**Verified live**, the same way as item 2h's dashboard change: ran the real Vite dev server,
stubbed `/tasks/my/<username>` and `/tasks/<id>/guacamole-token` in the browser console, and
confirmed both paths render correctly - the ready state's link has the exact expected
`href` (`data` URL-encoded correctly, confirmed via the browser's own accessibility tree, not
just the page text), and the error state shows the backend's message with a working retry. No
console errors beyond the expected pre-stub connection-refused noise from the page's other,
unrelated fetches (no real backend running here).

### 2j. The actual end-to-end relay, verified live with Docker (2026-10-24) - this is the big one

Docker became available on this machine (installed by Moeez, same system-level-change boundary as
the NSSM install). This closed out the single largest "written but never run" gap in all of R4 -
everything in items 2f-2i was real, tested code, but nothing had ever proven a token actually
gets a real SSH session through a real Guacamole instance until now.

**The workspace image (`node-agent/pool_runtime.py::WORKSPACE_DOCKERFILE`) - built, run, and a
real bug found and fixed**: `docker build` succeeded on the first try (the Dockerfile syntax was
right), and the custom-cont-init.d/custom-services.d hooks worked exactly as the LinuxServer.io
docs described - `docker logs` showed `10-sshd-setup: exited 0` and sshd running under s6
supervision. But the first real SSH login attempt (via `paramiko`, password auth) would have
authenticated successfully and then gone nowhere: the base image's `abc` user has `/bin/false` as
its login shell - fine for a user that only ever runs code-server as a background process, useless
for an interactive SSH session. Fixed with one line (`usermod -s /bin/bash abc`) in the Dockerfile.
Rebuilt, confirmed `abc:x:1000:1000::/config:/bin/bash` in `/etc/passwd`, and a real `paramiko`
login executed `whoami && pwd` successfully (`abc`, `/config`) using the exact per-session
password mechanism `start_workspace()` uses.

**Guacamole itself - brought up and it worked cleanly**: `docker compose up -d` (images bumped to
the current stable 1.6.0 from the originally-planned 1.5.5, now that this was actually being run
rather than just written). The `guacamole-auth-json` extension jar (not bundled in the Docker
image, downloaded from `dlcdn.apache.org` with explicit permission, SHA-256 verified against
Apache's own published checksum before use) loaded alongside the bundled PostgreSQL JDBC auth
exactly as `GUACAMOLE_HOME/extensions` is documented to work. Logs showed `"Encrypted JSON
Authentication" (json) loaded` and `"The Apache Guacamole web application has started"` with no
errors - the Postgres schema-sharing trick via the `guac-jdbc-init` named volume (item 2f's own
prediction about Docker's populate-on-first-mount behavior) worked exactly as expected too.

**The real test**: generated a token with the actual `backend/app/guacamole_gateway.py` code
(not a hand-rolled equivalent), pointing at the just-built workspace container's SSH port via
`host.docker.internal` (guacd runs inside the compose network, the test container was bound to
the host's own `127.0.0.1`, so this is the one piece of config that's prototype-specific and
wouldn't carry over to a different topology). `curl --data-urlencode "data=$TOKEN"
http://localhost:8080/guacamole/api/tokens` returned a real `authToken` with
`"dataSource":"json"` - Guacamole accepted a token our own code produced, not a hand-crafted
reference one. `GET .../api/session/data/json/connections` listed the exact connection the token
specified. Then, in the browser pane, navigating to
`http://localhost:8080/guacamole/#/?data=<token>` auto-opened the one connection and produced a
genuinely interactive SSH terminal - confirmed by typing `whoami` into the live canvas-rendered
terminal and getting `abc` back as real output, not just a connected-looking screen.

**What this proves, concretely**: the entire chain - token issuance code, Guacamole's JSON auth,
guacd's SSH relay, and the actual production workspace image - works end to end, for the first
time with real evidence instead of "written against the docs." **What it still doesn't prove**:
everything already flagged in item 2f's "what's still open" and this file's "real dependency"
section remains exactly as open as before - `host.docker.internal`/`127.0.0.1` reachability only
holds because guacd and the workspace container are on the same machine; a real lab PC on a
different machine, with no inbound ports, still has no solution here. This closes the "does the
mechanism work at all" question, not the "can this reach a real lab PC" question - those are
different problems, and only the first one was ever testable without solving the second.
