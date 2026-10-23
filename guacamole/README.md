# R4 Guacamole gateway - local prototype

**Verified live (2026-10-24)**, not just written against the docs: the full chain - a token
built by `backend/app/guacamole_gateway.py`, accepted by a real Guacamole instance, relayed
through `guacd` to a real SSH server running in the actual `crms-workspace` image - works end to
end. See `docs/handoff/R4_sessions_gateway_plan.md` item 2j for the full verification writeup,
including one real bug it found and fixed (the workspace image's `abc` user had `/bin/false` as
its login shell - SSH would authenticate and then go nowhere). The token crypto itself was
already cross-verified against the real `openssl` reference commands before this -
see `backend/tests/test_r4_guacamole_gateway.py`.

## What this is, and isn't

A **local, single-machine prototype** of Component 11's remote-access gateway (see
`docs/handoff/R4_sessions_gateway_plan.md` items 2f-2j and Part 3) - Guacamole in Docker, gated
by a token the backend issues, relaying a real SSH session into the actual workspace image. It is
deliberately **not**:

- A production deployment. It doesn't run on Render/Vercel/Supabase or any campus server, and
  shouldn't until the infrastructure question in the plan doc is actually decided.
- A replacement for the existing Cloudflare Quick Tunnel path. That's still how students reach
  their code-server workspace today (`node-agent/pool_runtime.py::start_workspace`) - this
  stack is a second, SSH-only path running alongside it, not instead of it.
- **A solution to reaching a lab PC with no inbound ports open** - this is the one thing item 2j's
  verification explicitly did NOT prove, and it's the real production blocker. `guacd` here
  reaches the workspace container via `host.docker.internal`/`127.0.0.1`, which only works
  because both are on the same machine. A lab PC on a different machine, with no inbound ports,
  still needs an outbound tunnel to wherever this stack runs - not built, not prototyped, a
  genuinely separate and harder problem than anything this prototype exercises.

## One-time setup

1. **Generate a secret key** and put it in two places (they must match exactly):
   ```
   openssl rand -hex 16
   ```
   - Copy `guacamole-home/guacamole.properties.example` to `guacamole-home/guacamole.properties`
     (gitignored) and set `json-secret-key` to the generated value.
   - Set the same value as `GUACAMOLE_JSON_SECRET_KEY` wherever the backend runs (an environment
     variable, not a file - see `backend/app/guacamole_gateway.py`).

2. **Get the `guacamole-auth-json` extension jar.** It is not bundled in the official
   `guacamole/guacamole` Docker image (unlike JDBC/LDAP, which that image can enable just from
   environment variables) - download the matching version from
   https://guacamole.apache.org/releases/ (binary release `guacamole-auth-json-1.6.0.tar.gz`,
   ~5.8MB; verify its SHA-256 against the checksum the release page links before trusting it),
   extract just the `.jar` from inside it, and place it at
   `guacamole-home/extensions/guacamole-auth-json-1.6.0.jar` (gitignored - this is a binary you
   fetch yourself, not something to vendor into the repo). The compose file pins `guacd`/
   `guacamole` to `1.6.0` too - keep all three in sync if you ever bump versions.

3. **Bring the stack up** (first run also initializes the Postgres schema automatically):
   ```
   docker compose up -d
   ```
   The schema files are copied out of the `guacamole` image into the shared `guac-jdbc-init`
   volume on its first start, and Postgres runs them from `/docker-entrypoint-initdb.d` on its
   own first start - confirmed working in the 2026-10-24 verification, no manual schema step
   needed. Check `docker compose logs guacamole` for `"The Apache Guacamole web application has
   started"` with the `json` extension listed as loaded.

4. **Build the workspace image and point guacd at a real target.** From `node-agent/`:
   ```
   # from a Python shell with pool_runtime importable:
   # python -c "import pool_runtime; print(pool_runtime.WORKSPACE_DOCKERFILE)" | docker build -t crms-workspace:2 -
   ```
   Then either start a real `remote`-mode session through the app (so a workspace container with
   sshd actually exists), or run one standalone for testing:
   ```
   docker run -d --name crms-ws-test -p 127.0.0.1:18443:8443 -p 127.0.0.1:12222:22 \
     -e PASSWORD=<pick one> -e PUID=1000 -e PGID=1000 crms-workspace:2
   ```
   `GUACAMOLE_PROTOTYPE_TARGET_HOST` (set on the backend, see `guacamole_gateway.py`) needs to be
   `host.docker.internal` for `guacd` (itself in the compose network) to reach a container
   published on the host's own `127.0.0.1` - this is prototype-specific plumbing, not something
   that carries over to a real multi-PC deployment.

## Using a token

The backend's `GET /tasks/<task_id>/guacamole-token` (added in `backend/app/student_routes.py`)
returns a token for the requesting student's own running session. Point a browser at:

```
http://localhost:8080/guacamole/#/?data=<url-encoded token>
```

and it auto-opens the one connection the token describes - confirmed working, including real
command execution inside the resulting terminal. Or POST it to `/api/tokens` directly (see
`json-auth.html`'s own curl example) to get a session back from Guacamole's REST API instead:

```
curl --data-urlencode "data=$TOKEN" http://localhost:8080/guacamole/api/tokens
```

## What's still open after this prototype works

- **The outbound-tunnel problem for a real, physically separate lab PC** - unchanged, and the
  real production blocker. Everything in this prototype assumes the gateway and the workspace
  container are reachable from each other directly; a real lab PC with no inbound ports has no
  path here yet.
- Idle detection and connect/disconnect logging - Guacamole's own connection history (under the
  JDBC auth module, once a real user database exists here, not just JSON auth) may cover some of
  this; not yet checked against this specific setup.
- The campus-server migration decision (whether this, or anything like it, ever runs anywhere but
  a dev box) - still nobody's explicit decision.
