# R4 handoff: VM slot pool, remote-access gateway, and pool consolidation

**For:** Maryam Tanveer
**Written by:** an AI assistant working with Muhammad Moeez on his own R3 scope, at his request,
as a starting point for you — not a substitute for your own work. Everything here is a plan to
review, adjust, and implement yourself; nothing in this file has been coded for you.

**Why consolidation is in this plan too:** the updated proposal's team-area table (Table 11)
credits pool consolidation to R4, but it was actually built under R3 earlier in this project
(with explicit approval at the time, before the proposal was rewritten). See
https://github.com/moeezmalik10/crms-deploy/issues/1 for the open discussion with the team about
how that should be credited. Regardless of how that's resolved, you should understand and be able
to maintain/extend that code if it's going to be presented as part of your component — this
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
