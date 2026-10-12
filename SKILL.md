---
name: not-mine
description: Before killing a process or port, deleting files, deploying, migrating, dropping or resetting a database, switching kube/cloud targets, or force-pushing, prove the target belongs to the current task. Runs a stdlib-only Python CLI that returns MINE / NOT_MINE / UNKNOWN with evidence (process start time vs session start, cwd, parent tree, uid; path inside workspace; DB host/name; kube context and namespace; AWS/GCP account; git branch and commits that would be lost; docker container origin). NOT_MINE or UNKNOWN means stop and ask the user. Use at the start of any coding session and before every destructive or state-changing command.
license: MIT
---

# Not Mine

Agents have killed production servers that sat on "their" port, `pkill`ed their own
IDE, wiped a sibling project's dev server, and ran destructive commands against
production hosts and databases. In every case the agent *assumed* the target was its
own. This skill replaces the assumption with a check.

The rule is simple: **before you destroy or change something, run `notmine check`. Act
only on `MINE`. On `NOT_MINE` or `UNKNOWN`, stop and ask the user, showing the evidence.**

## The CLI

`scripts/notmine.py` in this skill's directory. Python 3.8+, standard library only,
Linux and macOS. Call it as:

```bash
python3 <skill-dir>/scripts/notmine.py <command> ...
# or, if installed with pipx/pip:  notmine <command> ...
```

Below, `notmine` means either form.

| exit code | verdict | what you do |
|---|---|---|
| 0 | `MINE` | proceed |
| 10 | `NOT_MINE` | stop, show the evidence, ask the user |
| 11 | `UNKNOWN` | stop, show the evidence, ask the user |
| 2 | usage error | fix the command |

Add `--json` to any `check` for machine-readable output.

## Workflow

### 1. At the start of the session (once)

```bash
cd <task workspace>
notmine begin
```

This records the session start time, the workspace path, every process that already
exists, the agent's own process chain (so you never kill yourself), and the current
kube context. Processes that already existed are never yours.

If you know your agent's PID, pass `--root-pid <pid>` so its descendants count as yours.
The CLI also tries to detect common agent processes automatically.

### 2. When you create something you will later stop or delete

```bash
notmine register pid 4242        # a background job you started elsewhere
notmine register port 5173       # your dev server's port (records its pid too)
notmine register path /tmp/build-xyz
notmine register git agent/fix-login
notmine register docker my-test-db
```

Processes you start inside the workspace after `begin` are recognised without
registering. Registering is for things that live outside the workspace.

### 3. Before every destructive or state-changing action

| about to... | run first |
|---|---|
| `kill`, `pkill`, `killall`, stop a server, free a port | `notmine check port 3000` / `notmine check pid 1234` |
| `rm -rf`, `git clean`, delete/overwrite files, `rsync --delete`, `lftp mirror --delete` | `notmine check path <path>` for each root path |
| migrate, `DROP`, `TRUNCATE`, reset/seed a DB, `prisma migrate reset` | `notmine check db DATABASE_URL` (env var name or URL) |
| `kubectl delete/apply/scale/rollout`, `helm upgrade/uninstall` | `notmine check kube current -n <namespace>` |
| `terraform apply/destroy`, `aws ... delete`, `cdk deploy` | `notmine check aws current` (add `--live` to call STS) |
| `gcloud ... delete/deploy` | `notmine check gcp current` |
| `git push --force`, `--force-with-lease`, branch delete | `notmine check git origin/<branch>` |
| `docker rm/stop/kill`, `docker compose down -v` | `notmine check docker <container>` |

To stop a process, prefer the built-in safe kill. It re-verifies ownership right before
sending the signal (guards against PID reuse) and refuses anything not `MINE`:

```bash
notmine kill :3000          # by port
notmine kill 4242           # by pid
notmine kill :3000 --dry-run
```

**Never** use `pkill -f`, `killall`, `taskkill /IM`, or kill-by-name patterns. They
match processes you did not start, including the agent itself and the user's editor.

### 4. When the verdict is not MINE

Do not try another way to do the same thing, and do not "fix" the check. Stop and ask,
quoting the evidence. Example:

> `notmine` says port 8000 is **NOT MINE**: it is held by pid 5120
> (`gunicorn app:app`, cwd `/srv/billing`), which started 3 days before this session.
> I expected my backend on this port. Should I use a different port, or do you want me
> to stop that process?

`UNKNOWN` is not permission. It means the evidence is missing (no session, remote host
not in the allowlist, branch that predates the session). Ask.

If the user confirms a target is fine to touch, they can make it permanent in
`.notmine.json` (see below). Do not edit that file to unblock yourself without the
user's explicit go-ahead.

### 5. At the end

```bash
notmine end
```

## Configuration

Optional JSON at `<workspace>/.notmine.json` and/or `~/.config/notmine/config.json`:

```json
{
  "prod_patterns": ["prd", "customer"],
  "shared_patterns": ["demo"],
  "protected_paths": ["uploads", "data/customers", "*.env"],
  "protected_branches": ["main", "master", "release/*", "production"],
  "allow": {
    "db_hosts": ["devdb.internal", "localhost:5432"],
    "kube_contexts": ["team-dev-*"],
    "aws_accounts": ["111111111111"],
    "aws_profiles": ["sandbox"],
    "gcp_projects": ["myapp-dev-*"],
    "docker": ["myapp-test-*"]
  },
  "deny": {
    "aws_accounts": ["999999999999"],
    "kube_contexts": ["*-prod-*"],
    "db_hosts": ["localhost:15432"]
  }
}
```

Precedence: `deny` > `allow` > production patterns > local/dev detection > shared
patterns > `UNKNOWN`. Default production tokens: `prod`, `production`, `prd`, `live`
(matched as whole tokens, so `product` and `preprod` do not match). Default shared
tokens: `staging`, `stage`, `stg`, `shared`, `uat`, `preprod`, `qa`.

## What the verdicts are based on

- **pid / port**: running? your uid? ancestor of the checker (self-kill)? recorded as
  pre-existing at `begin`? start time vs session start; descendant of the agent root;
  cwd inside the workspace; registered (with start time, so a reused PID does not match).
  A port is `MINE` only if every listener on it is.
- **path**: real path after symlinks and `..`; inside the workspace (not the workspace
  itself, not `.git`, not `protected_paths`); temp files created during the session;
  everything else outside the workspace is `NOT_MINE`.
- **db**: URL or env var name; password redacted in output; prod tokens in host, db
  name or user; local host or SQLite in the workspace is `MINE`; remote hosts are
  `UNKNOWN` unless allowlisted.
- **kube**: parses kubeconfig (`$KUBECONFIG` or `~/.kube/config`) without kubectl;
  context, cluster, server and namespace; local clusters (kind, minikube, k3d,
  docker-desktop, localhost API) are `MINE`; system namespaces are `NOT_MINE`.
- **aws / gcp**: profile/project, role ARN and SSO account from the config files;
  optional live STS call; LocalStack-style local endpoints are `MINE`.
- **git**: protected branch patterns; commits on the remote branch by other authors that
  a force-push would delete; branch created during the session (reflog).
- **docker**: name, image, labels, creation time, compose working dir; remote
  `DOCKER_HOST` is `NOT_MINE`.

## Limits you must keep in mind

- A process the *user* starts in the workspace during your session looks exactly like
  yours. If the user said they are running something, ask before stopping it.
- A localhost port can be an SSH tunnel to production. Name-based checks cannot see that.
  If a DB URL uses an unusual local port, ask.
- Production resources with innocent names (`acme-main`) come back `UNKNOWN`, not
  `NOT_MINE`. That still means ask.
- Files inside the workspace are `MINE` unless listed in `protected_paths`. Deploy
  directories that hold user data need that list.
- This is a check you run, not a sandbox. It cannot stop a command you run without it.
