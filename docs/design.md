<img src="../design/logo/backtrack-mark.svg" alt="" width="64">

# Backtrack: design

Backtrack backs up Docker Compose stacks with restic. Every Docker host runs one agent container; all agents
write to one shared restic repo. One agent can also run the hub: a web UI and API for the whole fleet. Agents
never depend on the hub, the management plane (Komodo, Portainer, ...) or each other to back up and restore.

Status: **v2, approved 2026-11-15; implemented in v0.1.0, fixed after a real-host test in v0.1.1; set-aside folder tracking in v0.2.0; logo cache fix and larger favicon in v0.2.1**. Decisions are recorded in [Decisions](#decisions).

## Project

- Public repo `sloraris/backtrack`, MIT license.
- Image `ghcr.io/sloraris/backtrack`: `:vX.Y.Z` and `:latest` from tags `v*`, `:edge` from `main`. amd64 and arm64.
- Python 3.13, **standard library only** at runtime, on `python:3.13-slim` with a pinned restic binary copied
  from the official `restic/restic` image. restic does the heavy lifting; the agent is glue (restic, the Docker
  socket, a scheduler, a small HTTP server). No runtime dependencies to keep updated. Image about 60 MB.
- Generic: nothing in the code knows about any particular deployment, app or management plane. Site specifics
  are labels and env in the deploying repo.
- Dev and CI: `pytest` against a real local restic repo.

## Architecture

```
         browser, dashboards (read token), scripts (write token)
                               |
              backtrack.example (Traefik)  -> agent on host A, HUB=true
                               |   cluster-key signed calls
        +----------------+-----+-----------+----------------+
   agent host B     agent host C      agent host D      agent host E
   (API, no UI)     backup-<host>.example routes
        \________________ shared NFS share __________________/
                 /share/restic      restic repo (one for all)
                 /share/backtrack   presence files, maintenance lease
```

- **Agent** (every host): discovers stacks, runs the schedule, stop/start, backup, restore, auto-restore,
  restore test, alerts, heartbeat. Has a CLI (`docker exec backtrack backtrack <cmd>`) and an HTTP API, but no
  UI.
- **Hub** (`HUB=true`, normally one agent): serves the UI and the public API, and calls the other agents.
  It keeps no state of its own beyond settings and tokens; everything it shows comes live from agents and the
  repo. If the hub is down, agents keep backing up and alerting, and the CLI still works. Moving the hub means
  setting `HUB=true` on another agent.
- **Discovery:** every agent writes `<share>/backtrack/agents/<machine>.json` once a minute: machine name, its
  API URL (`AGENT_URL`), version, the stacks it sees with their state, current job, last run, maintenance
  status. The hub reads that folder. A file older than 3 minutes means the agent is offline.
- **Trust:**
  - Hub to agent: requests signed with a shared `CLUSTER_KEY` (HMAC-SHA256 over method, path, timestamp and
    body; 60 s clock skew allowed, nonces remembered for that window). Agents accept nothing else over HTTP.
  - People to hub: one admin password (`UI_PASSWORD`), session cookie (HttpOnly, Secure, SameSite=Strict, 12 h),
    CSRF tokens, rate-limited login.
  - Programs to hub: bearer tokens with a scope. `READ_TOKEN` (status, stacks, snapshots, jobs; for dashboards and monitoring) and
    `WRITE_TOKEN` (also backup and restore; for scripts). Each is compared in constant time and none works as
    another.
- **Network:** each agent's API is reached at its own route (`backup-<host>.example`), for example through the
  host's reverse proxy. A host outside the LAN can be reached over a VPN, with its route restricted to the VPN's
  range. Keep the hub off the public internet. `/healthz` needs no auth.

## Configuration

Env first. Secrets also accept `<NAME>_FILE`. Anything set in env is fixed: the hub UI shows it locked. Anything
not set in env can be changed from the hub UI and is saved on that agent in `<STATE_DIR>/settings.json`, so it
survives restarts and redeploys. The one exception is `MAINTENANCE`, see [Maintenance](#maintenance).

| Variable | Meaning | Default |
|---|---|---|
| `MACHINE` | this host's name (tags, UI, presence) | required |
| `AGENT_URL` | URL the hub uses to reach this agent | required with a hub |
| `CLUSTER_KEY` | agent-to-hub signing key (secret) | required with a hub |
| `RESTIC_REPOSITORY` | repo path in the container | `/share/restic` |
| `RESTIC_PASSWORD` | repo password (secret) | required |
| `SHARE_DIR` | presence files and lease | `/share/backtrack` |
| `APPDATA` | default stack data root, same path in and out of the container | `/opt/appdata` |
| `STATE_DIR` | agent settings and run history | `/opt/appdata/backtrack` |
| `SCRATCH` | restore-test scratch | `/opt/cache/backtrack` |
| `TZ` | schedule timezone | `UTC` |
| `BACKUP_SCHEDULE` | daily time `HH:MM` | `04:00` |
| `KEEP` | default retention | `daily=7,weekly=4,monthly=6` |
| `MAINTENANCE` | wants to be the prune/check agent | `false` |
| `PRUNE_SCHEDULE` | weekday and time | `sun 13:00` |
| `VERIFY_SCHEDULE` | day of month and time | `1 14:00` |
| `HUB` | serve the UI and public API | `false` |
| `UI_PASSWORD`, `READ_TOKEN`, `WRITE_TOKEN` | hub auth (secrets) | hub only |
| `DISCORD_WEBHOOK_URL` | failure alerts (secret) | off |
| `ALERT_WEBHOOK_URL` | failure alerts as plain JSON (secret) | off |
| `HEARTBEAT_URL` | push URL (Uptime Kuma, Healthchecks.io) (secret) | off |
| `HOOK_PRE_STOP_URL`, `HOOK_POST_START_URL` | global stop hooks (secret) | off |

## Stacks

The agent lists containers on `/var/run/docker.sock` and groups them by `com.docker.compose.project`. A project
is a Backtrack stack when any of its containers has `backtrack.mode`. Labels (read from any container of the
project; if two containers disagree, the stack is reported as misconfigured and skipped):

| Label | Meaning |
|---|---|
| `backtrack.mode=stop\|live` | stop the project's containers around the backup, or back up while running |
| `backtrack.path=/abs/path` | data folder; default `<APPDATA>/<project>` |
| `backtrack.keep=daily=3,weekly=2,monthly=0` | retention override |
| `backtrack.hook.pre-stop=<url>` | POSTed before stopping (stop mode) |
| `backtrack.hook.post-start=<url>` | POSTed after starting again |
| `backtrack.post-restore=<shell command>` | run on the host after a restore, before the stack starts |

- Excludes come from `<path>/.backupignore`, one pattern per line, read like `.gitignore`: a name without a
  slash (`cache/`, `*.log`) matches at any depth inside the folder, a path with a slash in the middle
  (`data/tmp`) is relative to the folder, a leading `/` is an absolute path, `!` negates and `#` is a comment.
  Backtrack anchors every pattern under the stack folder before handing it to restic, because restic on its own
  matches relative patterns against the whole absolute path (`cache/` would match `/opt/cache` and exclude
  everything; found in the first test on a real host).
- Named volumes are not supported in v1, and are not looked at.
- A stack whose folder is missing or empty is reported as an error and never backed up: an empty "latest"
  snapshot would make the next auto-restore wipe the stack.
- Stopped stacks are backed up and left stopped.
- A stack that no longer exists on any host keeps its snapshots until removed with `backtrack forget-stack`.
- Hook URLs often carry tokens. Use compose interpolation (`${VAR}` from the secrets file) so they stay out of
  git; they are still visible in `docker inspect`, like any env value.

## Backups

```
restic backup <path> --host <stack> \
  --tag stack=<stack> --tag machine=<MACHINE> --tag trigger=<nightly|manual|pre-restore|pre-migrate> \
  --exclude-file <.backupignore anchored under path> --exclude-caches --one-file-system
```

- `--host <stack>` makes each stack its own series. restic picks the parent by host and path, so a stack that
  moves to another machine keeps its history and stays incremental. Paths are the same inside and outside the
  container because data folders are mounted at their host paths.
- After each backup: `restic forget --host <stack> --tag trigger=nightly --tag trigger=manual
  --tag trigger=pre-migrate --keep-...` with that stack's retention, then `restic forget --host <stack>
  --tag trigger=pre-restore --keep-within 30d --keep-last 1`. Safety snapshots have their own retention so one
  taken late in a day can't take that day's daily slot from the nightly.
- "Latest" (restore, auto-restore, restore test) is the newest regular snapshot. A safety snapshot is used only
  when a stack has nothing else; to roll back a restore, pick it by id.
- Every restic call uses `--retry-lock 2h`, so backups and forgets wait out a prune instead of failing.

### Stop mode

1. Record which of the project's containers are running.
2. POST the pre-stop hooks (stack label, then global). A failed hook is a warning, the backup goes on.
3. `docker stop` them, dependents first (compose `depends_on` order reversed), 60 s timeout each.
4. Back up.
5. `finally`: start the containers that were running, in dependency order, then POST the post-start hooks.
   This runs when the backup failed and on SIGTERM. A failed start is its own alert.

Hook body: `{"event":"pre-stop"|"post-start","stack":..,"machine":..,"trigger":..,"snapshot":..|null,
"ok":true|false}`. 10 s timeout, one retry.

The management plane did not ask for these stops. Keep them short, and use the hooks to silence alerts that
would fire (for example Alertmanager silences, Uptime Kuma maintenance). Backing up the management plane itself is
just another `stop` stack: the agent talks to Docker directly.

## Schedule and jobs

- In-process scheduler in `TZ` (DST-safe). Missed runs are not replayed; the missing heartbeat alerts instead.
- Nightly: this host's stacks one at a time, in name order. Hosts run independently.
- One job at a time per agent. Manual jobs queue behind a running one. Jobs have ids, state and a log; the
  last 200 are kept in `STATE_DIR/history.jsonl`. A file lock in `STATE_DIR` is shared with CLI runs
  (`docker exec ... backtrack backup`), so a command never runs alongside the nightly job.
- On SIGTERM the agent finishes the running job before exiting (compose `stop_grace_period: 10m`), so a restore
  is never cut in half; queued jobs are dropped.

## Restore in place

`backtrack restore <stack> [--snapshot ID]`, or Restore in the hub with a typed confirmation (the stack name).

1. Stop the stack (with hooks, as for a backup). Restores always stop, whatever the mode.
2. Safety snapshot of the current folder (`trigger=pre-restore`) while it is stopped, unless it has no files.
   If it fails, nothing has changed: the stack starts again and the restore is reported as failed.
3. Rename `<path>` to `<path>.pre-restore`. An existing `.pre-restore` is first renamed to
   `.pre-restore.<timestamp>`. Each set-aside folder is recorded (see [Set-aside folders](#set-aside-folders)).
4. `restic restore <id>:<path> --target <path>`. The agent runs as root, so owners, groups and modes come back
   as recorded.
5. Carry over excluded data: every literal path in `.backupignore` (no wildcards, no negations; a bare name at
   any depth) that exists in `.pre-restore` and not in the restored folder is moved across. This keeps data that was never in the
   snapshot (large game worlds, media caches) where the stack expects it.
6. Run `backtrack.post-restore`, if set (see [Host commands](#host-commands)).
7. Start the containers that were running before (all of them if none existed), then post-start hooks.

A failure after step 3 leaves the stack stopped, alerts, and logs how to roll back (rename `.pre-restore` back).

## Set-aside folders

Every folder a restore sets aside is recorded in `STATE_DIR/prerestore.json`: stack, path, the folder it came from,
the restore job, the snapshot restored, the safety snapshot, whether the restore finished, and its size. A rotation
updates the record's path. A record whose folder was removed by hand is dropped. On the first start after an
upgrade, folders that earlier restores set aside are recorded from the job history.

Agents put the list, with a verdict for each folder, in their presence file, so the hub shows it on the stack's page
and warns when folders can be removed. A folder is **safe to remove** when:

- the restore finished (files restored and post-restore ran; otherwise the folder is kept for a manual rollback),
- every container of the stack is running, and those with a healthcheck report `healthy`,
- each container has stayed up for `PRERESTORE_SETTLE` (default 5 min) since its last start, so a restart loop
  never looks healthy, and that long has passed since the restore.

A stack with no containers on the host (a files-only restore) needs only the settle time.

Removing is a job on the agent that set the folder aside (hub button, `/api/v1`, or
`backtrack prerestore remove`), confirmed by typing the stack name. The agent takes the host lock and then:

1. accepts only a recorded path that matches `<folder>.pre-restore[.<timestamp>]` next to the folder it came
   from, is a real directory, and belongs to the stack's current data folder;
2. judges the stack again, now;
3. checks that the safety snapshot is still in the repository, since it is the rollback once the folder is gone;
4. deletes the folder and its record.

Nothing is removed on its own unless `PRERESTORE_AUTO_REMOVE` is set (e.g. `7d`). Then, once an hour, folders
older than that whose stack is safe are removed with the same checks.

## Auto-restore

`backtrack auto-restore <stack>` for a management plane's pre-deploy hook. It reads the stack's path from
`--path` if given, else `<APPDATA>/<stack>` (the stack's containers may not exist yet, so labels can't be read).

| Situation | Action | Exit |
|---|---|---|
| folder exists and is not empty | nothing | 0 |
| missing or empty, snapshot exists, stack not running elsewhere | restore latest (steps 4 and 6), log it | 0 |
| missing or empty, no snapshot | log "no snapshot, starts fresh" | 0 |
| another agent's presence file shows the stack running | refuse (split-brain guard) | 1 |
| repo unreachable or restore failed | log | 1 |

"Empty" means no files: a tree of empty folders (left by a `mkdir -p` in a pre-deploy step) still counts as empty.
The stack's `backtrack.post-restore` runs only when its containers already exist (a redeploy); on a host that has
never run the stack the labels can't be read yet, so chain the setup in the pre-deploy step instead
(`docker exec backtrack backtrack auto-restore myapp && bash setup.sh`).

Failing closed is deliberate: a fresh start on top of a failed restore creates new state and a new "latest"
that hides the real one. The agent must be deployed on a host before the stacks that use auto-restore. The
pre-deploy step is `docker exec backtrack backtrack auto-restore <stack>`. Restoring the management plane on a
fresh host is the same command over SSH, then `docker compose up -d`.

## Migration

Moving a stack from host A to B:

1. Hub: "Back up for migration" on A: a normal stop-mode backup tagged `trigger=pre-migrate`; the stack starts
   again afterwards. Backtrack does not leave it stopped, since the management plane would see it as unhealthy.
2. Stop or remove the stack on A through the management plane.
3. Point the stack at B in the management plane and deploy. B's auto-restore finds no folder and
   restores the pre-migrate snapshot. If A's agent still reports the stack running, auto-restore refuses and
   the deploy fails, so the stack never runs on two hosts with diverging data.

Writes on A between steps 1 and 2 are lost; keep the gap short.

## Host commands

`backtrack.post-restore` runs on the host, not in the agent container: the agent starts a short-lived helper
container from its own image (`--privileged --pid=host --rm`) running
`nsenter -t 1 -m -u -i -n -p -- sh -c '<command>'`, with `BACKTRACK_STACK`, `BACKTRACK_PATH` and
`BACKTRACK_SNAPSHOT` in its env. Always enabled: anyone who can set labels on a container on this host can
already reach the Docker socket. Output goes to the job log; a non-zero exit fails the restore before the stack
starts.

For example, a stack whose host-side bind mounts still point at the renamed folder after a restore can redo
them:

```yaml
- backtrack.post-restore=umount /etc/myapp /var/lib/myapp 2>/dev/null; bash /srv/myapp/setup.sh
```

## Maintenance

Prune and `restic check` are repo-wide and take the exclusive lock, so exactly one agent does them. That agent
is named in a lease, `<share>/backtrack/maintenance.json` (`machine`, `assigned_by`, `renewed_at`).

- An agent prunes and checks only while the lease names it. It renews the lease daily.
- **Hub active:** only the hub assigns the lease (from the UI). Every other agent shows maintenance as locked.
- **No hub:** an agent with `MAINTENANCE=true` claims the lease if it is free or not renewed for 8 days. A
  second agent with `MAINTENANCE=true` is ignored while the lease is held, and the hub shows a warning for it.
  This is the one place env does not win.
- Weekly prune (`PRUNE_SCHEDULE`, default Sunday 13:00, away from the backup window).
- Monthly repo check: `restic check --read-data-subset=<month>/12`, so all data is read once a year.

## Restore test

Monthly (`VERIFY_SCHEDULE`), every agent restores the latest snapshot of one random stack of its own to
`SCRATCH/restore-test/<stack>`, compares file count and size with `restic ls`/`stats`, and deletes the copy.
Failure alerts.

## Alerts and heartbeat

- Failures go to Discord (one embed per failure: stack, machine, step, last lines of restic's stderr) and/or the
  plain JSON webhook. A nightly summary is posted only when something failed. Successes are silent.
- After each nightly run the agent calls `HEARTBEAT_URL` with `status=up` or `status=down` and a short message
  (Kuma's push format; other services ignore the extra query). Set the monitor's interval to about 26 h so a run
  that never happens alerts.
- Secrets, tokens and webhook URLs are never logged; URLs show as host only.

## Hub UI

Server-rendered HTML with a little vanilla JS and one neutral stylesheet that follows the browser's light or
dark setting.

- Fleet: agents (online, version, current job, last nightly), warnings (offline agent, no maintenance lease, two
  maintenance claims).
- Stacks: all stacks across agents (machine, mode, last backup, size added, snapshot count). Back up now; Back
  up for migration.
- Stack: snapshots from the repo (any machine), Restore with typed confirmation, set-aside folders with their
  verdict and a Remove button (typed confirmation) once safe, job logs.
- Agent: settings (env-set ones locked), maintenance lease.

## APIs

Agent API (hub only, signed): `GET /v1/status`, `GET /v1/stacks`, `GET /v1/stacks/{s}/snapshots`,
`POST /v1/stacks/{s}/backup`, `POST /v1/stacks/{s}/restore`, `GET /v1/jobs/{id}`, `GET|PUT /v1/settings`,
`GET /v1/prerestore`, `POST /v1/stacks/{s}/prerestore/remove`.

Hub API (bearer token or session), under `/api/v1`:

| Method | Path | Scope |
|---|---|---|
| GET | `/status` | read: fleet summary, warnings, maintenance holder |
| GET | `/agents` | read |
| GET | `/stacks` | read |
| GET | `/stacks/{stack}/snapshots` | read |
| GET | `/jobs/{machine}/{id}` | read |
| POST | `/stacks/{stack}/backup` `{"trigger":"manual"\|"pre-migrate"}` | write |
| POST | `/stacks/{stack}/restore` `{"snapshot":"latest"\|ID,"confirm":"<stack>"}` | write |
| GET | `/prerestore` | read: set-aside folders, each with `state` (`settling`, `safe`, `unhealthy`, `failed`) |
| POST | `/stacks/{stack}/prerestore/remove` `{"path":"…","confirm":"<stack>","machine":"…"}` | write; `machine` only when two agents have the path |

A dashboard or monitor needs only the read token, for `/api/v1/status` and `/api/v1/stacks`.

## Deploying

`examples/compose.yaml` in the repo shows an agent, and `examples/compose.hub.yaml` the hub agent:

```yaml
name: backtrack
services:
  agent:
    image: ghcr.io/sloraris/backtrack:0.2.1
    container_name: backtrack
    restart: unless-stopped
    environment:
      MACHINE: alpha
      AGENT_URL: https://backup-alpha.example.com
      TZ: Etc/UTC
      RESTIC_PASSWORD: ${RESTIC_PASSWORD}
      CLUSTER_KEY: ${CLUSTER_KEY}
      DISCORD_WEBHOOK_URL: ${DISCORD_WEBHOOK_URL}
      HEARTBEAT_URL: ${HEARTBEAT_URL}
      # hub agent only:
      HUB: "true"
      UI_PASSWORD: ${UI_PASSWORD}
      READ_TOKEN: ${READ_TOKEN}
      WRITE_TOKEN: ${WRITE_TOKEN}
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /opt/appdata:/opt/appdata
      - /opt/cache/backtrack:/opt/cache/backtrack
      - nas:/share
    labels: [reverse proxy labels: backup-alpha.example.com for the agent API, backtrack.example.com for the hub]
    networks: [traefik]
volumes:
  nas:             # a mount definition, not stored state
    driver: local
    driver_opts:
      type: nfs
      o: addr=<nas-ip>,nfsvers=4.1,hard,timeo=600
      device: ":<export-path>"
```

Around the deployment: every host must reach the NAS over NFS and the hub must reach every agent's route (a host
outside the LAN can do both over a VPN), one heartbeat monitor per host, and DNS for the hub and agent routes.

### NFS and root squash

restic stores file owners in the repo's metadata, not as owners of files on the share, so root squash on the NAS
does not lose ownership. It only means repo files are written as the squashed user, so the export must be
writable by that user (or use `no_root_squash` for the agent hosts). Restores write to local disk as root.

## Testing

pytest, with a real restic repo in a temp dir and a fake Docker client (records stop/start, can fail on demand):

- backup: `--host`, tags, excludes, empty-folder refusal, start after a failed backup, stopped stacks stay
  stopped, retention override, hooks called and hook failure tolerated.
- restore: safety snapshot, `.pre-restore` rename and rotation, owners and modes kept (CI runs as root),
  excluded-path carry-over, post-restore before start, failure leaves stack stopped.
- set-aside folders: recorded on restore and rotation, verdicts (settling, safe, unhealthy, restart, failed
  restore), removal refused while not safe, for unrecorded or tampered paths and without the safety snapshot,
  auto-remove, adoption from job history.
- auto-restore: every row of the table above, including the split-brain guard via a fake presence file.
- migration: backup tagged on machine A, restore on B uses the same series and B's next backup uses A's
  snapshot as parent.
- maintenance lease: claim, renew, stale takeover, hub assignment, second claimant ignored.
- scheduler across DST; signing (valid, expired, replayed, wrong key); token scopes; env-locked settings.

CI: tests on every push and PR; image build and push to GHCR from `main` and tags.

## Decisions

| # | Decision |
|---|---|
| 1 | Public, generic, MIT; repo/image/CLI named `backtrack`. |
| 2 | Python 3.13 stdlib only, restic binary in the image. |
| 3 | Agent everywhere; hub is a mode of the same image, on one agent. Outside tools only observe, with the read token. |
| 4 | Discovery by presence files on the shared share. |
| 5 | Hub to agent signed with a cluster key; hub tokens scoped read/write; no per-agent tokens. |
| 6 | Agent APIs behind reverse-proxy routes; hosts outside the LAN reach the API and NFS over a VPN, routes restricted to its range. |
| 7 | Labels with a fixed `backtrack.` prefix; data path by convention with a label override; no named volumes in v1. |
| 8 | No database dumps; databases use stop mode, with webhook hooks from labels (or global env) to silence alerts. A failed hook warns and the backup continues. |
| 9 | Restore in place takes a safety snapshot first and carries excluded literal paths over from `.pre-restore`. |
| 10 | Post-restore host commands always enabled. |
| 11 | Migration: backup only (tagged pre-migrate, stack restarted) plus a split-brain guard in auto-restore. |
| 12 | Settings: env locks, UI fills the gaps, saved per agent. |
| 13 | One maintenance agent via a lease on the share; the hub assigns it while active. |
| 14 | Alerts: Discord and a generic JSON webhook; heartbeat to any push URL. |
| 15 | One neutral UI theme (light/dark). The original theme was dropped after v0.1.0 to keep Backtrack standalone. |
| 16 | Set-aside folders are recorded and removed only on request (or opt-in auto-remove) once the stack is healthy and the safety snapshot exists. |
