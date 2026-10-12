#!/usr/bin/env python3
"""notmine - verify a target belongs to the current task before you kill,
delete, deploy, migrate, drop or force-push it.

Stdlib only. Linux and macOS. Python 3.8+.

Verdicts and exit codes:
    MINE      0   evidence says the target belongs to this session/task
    NOT_MINE  10  evidence says it belongs to someone/something else -> ask the user
    UNKNOWN   11  not enough evidence either way -> ask the user
    (usage errors exit 2)
"""

import argparse
import configparser
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit, unquote

__version__ = "0.1.0"

MINE, NOT_MINE, UNKNOWN = "MINE", "NOT_MINE", "UNKNOWN"
EXIT = {MINE: 0, NOT_MINE: 10, UNKNOWN: 11}

IS_LINUX = sys.platform.startswith("linux") and os.path.isdir("/proc/self")
# Start-time precision: /proc gives ~10ms, `ps -o lstart` gives 1s.
START_SLACK = 0.1 if IS_LINUX else 1.0

DEFAULT_PROD = [r"prod", r"production", r"prd", r"live"]
DEFAULT_SHARED = [r"staging", r"stage", r"stg", r"shared", r"uat", r"preprod", r"pre-prod", r"qa"]
DEFAULT_LOCAL_HOSTS = ["localhost", "127.0.0.1", "::1", "0.0.0.0", "*.localhost", "*.local",
                       "host.docker.internal", "127.*"]
DEFAULT_DEV = [r"dev", r"development", r"test", r"testing", r"local", r"sandbox", r"ci", r"tmp", r"scratch"]
DEFAULT_PROTECTED_BRANCHES = ["main", "master", "trunk", "develop", "release", "release/*",
                              "releases/*", "prod", "prod*", "production", "live", "stable"]
LOCAL_KUBE_PREFIXES = ("kind-", "k3d-", "minikube", "docker-desktop", "docker-for-desktop",
                       "rancher-desktop", "orbstack", "colima", "microk8s")
SYSTEM_NAMESPACES = ("kube-system", "kube-public", "kube-node-lease", "istio-system",
                     "cert-manager", "monitoring", "ingress-nginx")
AGENT_NAMES = ("claude", "codex", "gemini", "cursor", "cursor-agent", "opencode", "aider",
               "neovarch", "goose", "amp", "cline", "crush", "qwen", "kiro", "copilot")


# --------------------------------------------------------------------------- utils

def _now():
    return time.time()


def _run(cmd, cwd=None, timeout=10, env=None):
    """Run a command, return (rc, stdout, stderr). Never raises for missing binaries."""
    try:
        e = dict(os.environ)
        e["LC_ALL"] = "C"
        if env:
            e.update(env)
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=e)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)


def _real(path):
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def _inside(child, parent):
    child, parent = _real(child), _real(parent)
    if child == parent:
        return True
    return child.startswith(parent.rstrip(os.sep) + os.sep)


def _tokens_match(text, patterns):
    """Return the first pattern that matches `text` as a whole token.

    A token boundary is anything that is not a letter, so `prod-db`, `db_prod`,
    `app.prod.internal` match `prod` but `product` and `preprod` do not."""
    if not text:
        return None
    low = str(text).lower()
    for pat in patterns:
        if re.search(r"(?:^|[^a-z])(?:%s)(?:[^a-z]|$)" % pat, low):
            return pat
    return None


def _glob_any(value, globs):
    if value is None:
        return None
    for g in globs or []:
        if fnmatch.fnmatchcase(str(value).lower(), str(g).lower()):
            return g
    return None


def _redact_url(url):
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.password:
        netloc = parts.netloc.replace(":" + parts.password + "@", ":***@")
        return parts._replace(netloc=netloc).geturl()
    return url


class Verdict(object):
    def __init__(self, kind, target):
        self.kind = kind
        self.target = target
        self.verdict = None
        self.reason = ""
        self.evidence = []

    def ev(self, msg):
        self.evidence.append(msg)
        return self

    def done(self, verdict, reason):
        if self.verdict is None:
            self.verdict = verdict
            self.reason = reason
        return self

    def to_dict(self):
        return {"kind": self.kind, "target": self.target, "verdict": self.verdict,
                "reason": self.reason, "evidence": self.evidence,
                "action": {MINE: "proceed", NOT_MINE: "stop and ask the user",
                           UNKNOWN: "stop and ask the user"}[self.verdict],
                "exit_code": EXIT[self.verdict]}


# --------------------------------------------------------------------------- config

def load_config(workspace=None):
    cfg = {"prod_patterns": list(DEFAULT_PROD), "shared_patterns": list(DEFAULT_SHARED),
           "dev_patterns": list(DEFAULT_DEV), "local_hosts": list(DEFAULT_LOCAL_HOSTS),
           "protected_branches": list(DEFAULT_PROTECTED_BRANCHES), "protected_paths": [],
           "allow": {}, "deny": {},
           "_sources": []}
    paths = []
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    paths.append(os.path.join(xdg, "notmine", "config.json"))
    if workspace:
        paths.append(os.path.join(workspace, ".notmine.json"))
    if os.environ.get("NOTMINE_CONFIG"):
        paths.append(os.environ["NOTMINE_CONFIG"])
    for p in paths:
        if not os.path.isfile(p):
            continue
        try:
            with open(p) as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            raise SystemExit("notmine: cannot read config %s: %s" % (p, exc))
        cfg["_sources"].append(p)
        replace = bool(data.get("replace_default_patterns"))
        for key in ("prod_patterns", "shared_patterns", "dev_patterns", "local_hosts",
                    "protected_branches", "protected_paths"):
            if key in data:
                cfg[key] = list(data[key]) if replace or key == "protected_branches" else cfg[key] + list(data[key])
        for key in ("allow", "deny"):
            for k, v in (data.get(key) or {}).items():
                cfg[key].setdefault(k, [])
                cfg[key][k] = cfg[key][k] + [str(x) for x in v]
    return cfg


# --------------------------------------------------------------------------- session registry

def _state_dir():
    base = os.environ.get("NOTMINE_HOME")
    if not base:
        xdg = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
        base = os.path.join(xdg, "notmine")
    return os.path.join(base, "sessions")


def _session_path(workspace):
    h = hashlib.sha1(_real(workspace).encode()).hexdigest()[:16]
    return os.path.join(_state_dir(), h + ".json")


def find_session(cwd=None):
    """Find the session whose workspace contains cwd (nearest ancestor wins)."""
    explicit = os.environ.get("NOTMINE_SESSION_FILE")
    if explicit:
        if os.path.isfile(explicit):
            with open(explicit) as fh:
                return json.load(fh), explicit
        return None, None
    d = _real(cwd or os.getcwd())
    while True:
        p = _session_path(d)
        if os.path.isfile(p):
            try:
                with open(p) as fh:
                    return json.load(fh), p
            except (OSError, ValueError):
                return None, None
        parent = os.path.dirname(d)
        if parent == d:
            return None, None
        d = parent


def save_session(sess, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(sess, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- processes

_BOOT = None


def _boot_time():
    """Boot time on the time.time() clock. /proc/uptime is used instead of the
    integer `btime` in /proc/stat, which can be off by up to a second."""
    global _BOOT
    if _BOOT is None:
        try:
            with open("/proc/uptime") as fh:
                _BOOT = time.time() - float(fh.read().split()[0])
        except (OSError, ValueError, IndexError):
            with open("/proc/stat") as fh:
                for line in fh:
                    if line.startswith("btime"):
                        _BOOT = float(line.split()[1])
                        break
    return _BOOT


def proc_info(pid):
    """Return dict(pid, ppid, uid, start, cwd, cmd) or None if not running."""
    pid = int(pid)
    if IS_LINUX:
        try:
            with open("/proc/%d/stat" % pid) as fh:
                stat = fh.read()
        except OSError:
            return None
        rest = stat[stat.rfind(")") + 2:].split()
        ppid = int(rest[1])
        start = _boot_time() + int(rest[19]) / float(os.sysconf("SC_CLK_TCK"))
        try:
            uid = os.stat("/proc/%d" % pid).st_uid
        except OSError:
            uid = None
        try:
            cwd = os.readlink("/proc/%d/cwd" % pid)
        except OSError:
            cwd = None
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            cmd = ""
        if not cmd:
            cmd = stat[stat.find("(") + 1:stat.rfind(")")]
        return {"pid": pid, "ppid": ppid, "uid": uid, "start": start, "cwd": cwd, "cmd": cmd}
    rc, out, _ = _run(["ps", "-o", "ppid=,uid=,lstart=,command=", "-p", str(pid)])
    if rc != 0 or not out.strip():
        return None
    f = out.strip().split(None, 7)
    try:
        ppid, uid = int(f[0]), int(f[1])
        start = time.mktime(time.strptime(" ".join(f[2:7]), "%a %b %d %H:%M:%S %Y"))
    except (ValueError, IndexError):
        return None
    cmd = f[7] if len(f) > 7 else ""
    cwd = None
    rc, out, _ = _run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
    for line in out.splitlines():
        if line.startswith("n"):
            cwd = line[1:]
    return {"pid": pid, "ppid": ppid, "uid": uid, "start": start, "cwd": cwd, "cmd": cmd}


def all_pids():
    """{pid: start_time} for every visible process."""
    res = {}
    if IS_LINUX:
        for name in os.listdir("/proc"):
            if name.isdigit():
                info = proc_info(int(name))
                if info:
                    res[info["pid"]] = info["start"]
        return res
    rc, out, _ = _run(["ps", "-axo", "pid=,lstart="])
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 6:
            try:
                res[int(f[0])] = time.mktime(time.strptime(" ".join(f[1:6]), "%a %b %d %H:%M:%S %Y"))
            except ValueError:
                pass
    return res


def ancestors(pid):
    chain, seen = [], set()
    cur = proc_info(pid)
    while cur and cur["ppid"] and cur["pid"] not in seen:
        seen.add(cur["pid"])
        parent = proc_info(cur["ppid"])
        if not parent:
            break
        chain.append(parent)
        cur = parent
    return chain


def _is_descendant(pid, root_pid, root_start):
    info = proc_info(pid)
    seen = set()
    while info and info["pid"] not in seen:
        seen.add(info["pid"])
        if info["pid"] == root_pid:
            return abs(info["start"] - root_start) <= START_SLACK + 0.01
        if not info["ppid"]:
            return False
        info = proc_info(info["ppid"])
    return False


def _detect_agent_root(chain):
    for p in chain:
        exe = (p["cmd"] or "").split()
        if not exe:
            continue
        names = [os.path.basename(x).lower() for x in exe[:3]]
        for n in names:
            stem = re.sub(r"(\.exe|\.js|\.mjs|\.cjs|\.py)$", "", n)
            if stem in AGENT_NAMES:
                return p
    return None


def listeners(port):
    """Return list of dicts {pid, uid} listening on TCP `port`. pid may be None."""
    port = int(port)
    found = []
    if IS_LINUX:
        inodes = {}
        for fn in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(fn) as fh:
                    next(fh)
                    for line in fh:
                        f = line.split()
                        if f[3] != "0A":
                            continue
                        if int(f[1].split(":")[1], 16) == port:
                            inodes[f[9]] = int(f[7])
            except (OSError, StopIteration):
                pass
        if not inodes:
            return found
        owners = {}
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            fd_dir = "/proc/%s/fd" % name
            try:
                for fd in os.listdir(fd_dir):
                    try:
                        link = os.readlink(os.path.join(fd_dir, fd))
                    except OSError:
                        continue
                    if link.startswith("socket:["):
                        ino = link[8:-1]
                        if ino in inodes and ino not in owners:
                            owners[ino] = int(name)
            except OSError:
                continue
        for ino, uid in inodes.items():
            found.append({"pid": owners.get(ino), "uid": uid})
        return found
    rc, out, _ = _run(["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-Fpu"])
    cur = {}
    for line in out.splitlines():
        if line.startswith("p"):
            if cur:
                found.append(cur)
            cur = {"pid": int(line[1:]), "uid": None}
        elif line.startswith("u") and cur:
            cur["uid"] = int(line[1:])
    if cur:
        found.append(cur)
    if not found and _port_in_use(port):
        found.append({"pid": None, "uid": None})
    return found


def _port_in_use(port):
    for fam, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        s = socket.socket(fam, socket.SOCK_STREAM)
        s.settimeout(0.3)
        try:
            if s.connect_ex((host, port)) == 0:
                return True
        except OSError:
            pass
        finally:
            s.close()
    return False


# --------------------------------------------------------------------------- checks

def check_pid(pid, sess, cfg, v=None):
    v = v or Verdict("pid", str(pid))
    pid = int(pid)
    me = os.getpid()
    if pid in (0, 1):
        v.ev("pid %d is the init/kernel process" % pid)
        return v.done(NOT_MINE, "system process")
    info = proc_info(pid)
    if not info:
        v.ev("no process with pid %d is running (or it is not visible)" % pid)
        return v.done(UNKNOWN, "process not found")
    v.ev("cmd: %s" % (info["cmd"][:200] or "?"))
    v.ev("uid: %s, ppid: %s, cwd: %s" % (info["uid"], info["ppid"], info["cwd"] or "unreadable"))
    v.ev("started: %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info["start"])))
    if pid == me:
        return v.done(NOT_MINE, "this is the notmine process itself")
    live_chain = ancestors(me)
    for a in live_chain:
        if a["pid"] == pid:
            v.ev("pid %d is an ancestor of the process running this check" % pid)
            return v.done(NOT_MINE, "killing it would kill your own agent/shell/terminal")
    if info["uid"] is not None and info["uid"] != os.getuid():
        v.ev("owned by uid %s, you are uid %s" % (info["uid"], os.getuid()))
        return v.done(NOT_MINE, "process belongs to another user")
    if not sess:
        v.ev("no notmine session covers %s (run `notmine begin`)" % os.getcwd())
        return v.done(UNKNOWN, "no session registry; ownership cannot be proven")
    # Recorded ancestors at begin time (agent, terminal, IDE) are never mine either.
    for a in sess.get("ancestors", []):
        if a["pid"] == pid and abs(a["start"] - info["start"]) <= START_SLACK + 0.01:
            v.ev("pid %d (%s) was an ancestor of `notmine begin`" % (pid, a.get("cmd", "")[:60]))
            return v.done(NOT_MINE, "this is your agent/terminal/IDE process, not a task process")
    for r in sess.get("registered", {}).get("pid", []):
        if int(r["pid"]) == pid:
            if abs(float(r["start"]) - info["start"]) <= START_SLACK + 0.01:
                v.ev("registered by this session via `notmine register pid %d`" % pid)
                return v.done(MINE, "explicitly registered by this session")
            v.ev("pid %d was registered, but start time differs: pid was reused" % pid)
    started_before = info["start"] < sess["start"] - START_SLACK
    pre = sess.get("preexisting", {}).get(str(pid))
    if pre is not None and abs(pre - info["start"]) <= START_SLACK + 0.01:
        v.ev("pid %d already existed when the session began" % pid)
        return v.done(NOT_MINE, "process predates this session")
    if started_before:
        v.ev("process started %.1fs before the session began" % (sess["start"] - info["start"]))
        return v.done(NOT_MINE, "process predates this session")
    v.ev("process started %.1fs after the session began" % (info["start"] - sess["start"]))
    root = sess.get("root")
    if root and _is_descendant(pid, root["pid"], root["start"]):
        v.ev("descendant of session root pid %d (%s)" % (root["pid"], root.get("cmd", "")[:60]))
        return v.done(MINE, "started after session begin by the agent's process tree")
    if info["cwd"] and _inside(info["cwd"], sess["workspace"]):
        v.ev("cwd %s is inside workspace %s" % (info["cwd"], sess["workspace"]))
        return v.done(MINE, "started after session begin, running inside the workspace")
    if info["cwd"]:
        v.ev("cwd %s is outside workspace %s" % (info["cwd"], sess["workspace"]))
    return v.done(UNKNOWN, "new process, but not inside the workspace or the agent's tree")


def check_port(port, sess, cfg):
    v = Verdict("port", str(port))
    try:
        port = int(str(port).lstrip(":"))
    except ValueError:
        raise SystemExit("notmine: port must be a number")
    owners = listeners(port)
    if sess:
        before = sess.get("ports_at_begin", {}).get(str(port))
        if before:
            v.ev("port %d was already listening when the session began (pids %s)" % (port, before))
    if not owners:
        v.ev("nothing is listening on TCP port %d" % port)
        return v.done(UNKNOWN, "no listener; nothing to kill")
    for reg in (sess or {}).get("registered", {}).get("port", []):
        if int(reg["port"]) == port:
            v.ev("port %d registered by this session" % port)
    verdicts = []
    for o in owners:
        if o["pid"] is None:
            if o["uid"] is not None and o["uid"] != os.getuid():
                v.ev("listener owned by uid %s (you are uid %s)" % (o["uid"], os.getuid()))
                verdicts.append(NOT_MINE)
            else:
                v.ev("listener found but its process is not visible to you")
                verdicts.append(UNKNOWN)
            continue
        sub = check_pid(o["pid"], sess, cfg, Verdict("pid", str(o["pid"])))
        v.ev("listener pid %d -> %s (%s)" % (o["pid"], sub.verdict, sub.reason))
        v.evidence.extend("  " + e for e in sub.evidence)
        verdicts.append(sub.verdict)
    if NOT_MINE in verdicts:
        return v.done(NOT_MINE, "port %d is held by a process that is not yours" % port)
    if UNKNOWN in verdicts:
        return v.done(UNKNOWN, "ownership of port %d cannot be proven" % port)
    return v.done(MINE, "every listener on port %d belongs to this session" % port)


def _temp_roots():
    roots = {tempfile.gettempdir(), "/tmp", "/var/tmp"}
    if os.environ.get("TMPDIR"):
        roots.add(os.environ["TMPDIR"])
    return sorted({_real(r) for r in roots if os.path.isdir(r)})


def check_path(path, sess, cfg):
    v = Verdict("path", path)
    rp = _real(path)
    if rp != os.path.abspath(os.path.expanduser(path)):
        v.ev("resolves to %s (symlinks / .. followed)" % rp)
    else:
        v.ev("absolute path: %s" % rp)
    workspace = sess["workspace"] if sess else None
    if not workspace:
        rc, out, _ = _run(["git", "rev-parse", "--show-toplevel"])
        workspace = _real(out.strip()) if rc == 0 and out.strip() else _real(os.getcwd())
        v.ev("no session; assuming workspace = %s" % workspace)
    else:
        v.ev("workspace: %s" % workspace)
    home = _real("~")
    for r in (sess or {}).get("registered", {}).get("path", []):
        if _inside(rp, r["path"]):
            v.ev("inside path registered by this session: %s" % r["path"])
            return v.done(MINE, "registered by this session")
    if rp in ("/", home) or rp == _real(workspace) or _inside(workspace, rp):
        v.ev("target is the workspace itself, one of its parents, / or $HOME")
        return v.done(NOT_MINE if rp in ("/", home) else UNKNOWN,
                      "deleting this would remove the whole workspace or more")
    if _inside(rp, workspace):
        rel = os.path.relpath(rp, workspace)
        for g in cfg.get("protected_paths", []):
            parts = rel.split(os.sep)
            if any(fnmatch.fnmatchcase(os.sep.join(parts[:i]), g) for i in range(1, len(parts) + 1)):
                v.ev("%s matches protected_paths %r in config" % (rel, g))
                return v.done(NOT_MINE, "protected path inside the workspace")
        if rel.split(os.sep)[0] == ".git":
            v.ev("target is inside .git (repository history)")
            return v.done(UNKNOWN, "repository metadata; ask before touching")
        return v.done(MINE, "inside the task workspace")
    for t in _temp_roots():
        if _inside(rp, t) and rp != t:
            v.ev("inside temp dir %s" % t)
            if not os.path.lexists(rp):
                return v.done(UNKNOWN, "temp path does not exist")
            st = os.lstat(rp)
            born = getattr(st, "st_birthtime", None) or st.st_ctime
            if st.st_uid != os.getuid():
                v.ev("owned by uid %s" % st.st_uid)
                return v.done(NOT_MINE, "temp file of another user")
            if sess and born >= sess["start"] - START_SLACK:
                v.ev("created/changed after the session began")
                return v.done(MINE, "temp file created during this session")
            v.ev("existed before the session began" if sess else "no session to compare age")
            return v.done(UNKNOWN if not sess else NOT_MINE, "temp file not created by this session")
    if _inside(rp, home):
        v.ev("outside the workspace, under $HOME")
    else:
        v.ev("outside the workspace and $HOME (system location)")
    return v.done(NOT_MINE, "outside the task workspace")


def _classify_name(fields, cfg, v, allow_key=None, deny_key=None, value_for_lists=None):
    """Shared deny > allow > prod > shared logic. Returns a verdict or None."""
    vals = [x for x in fields if x]
    if deny_key:
        for x in vals + ([value_for_lists] if value_for_lists else []):
            g = _glob_any(x, cfg["deny"].get(deny_key))
            if g:
                v.ev("%r matches deny.%s %r in config" % (x, deny_key, g))
                return NOT_MINE, "explicitly denied in notmine config"
    if allow_key:
        for x in vals + ([value_for_lists] if value_for_lists else []):
            g = _glob_any(x, cfg["allow"].get(allow_key))
            if g:
                v.ev("%r matches allow.%s %r in config" % (x, allow_key, g))
                return MINE, "explicitly allowed in notmine config"
    for x in vals:
        p = _tokens_match(x, cfg["prod_patterns"])
        if p:
            v.ev("%r matches production pattern %r" % (x, p))
            return NOT_MINE, "looks like production"
    for x in vals:
        p = _tokens_match(x, cfg["shared_patterns"])
        if p:
            v.ev("%r matches shared-environment pattern %r" % (x, p))
            return UNKNOWN, "looks like a shared (staging/QA) environment"
    return None


def _is_local_host(host, cfg):
    if not host or host.startswith("/"):
        return True  # unix socket / default local connection
    h = host.lower().strip("[]")
    return bool(_glob_any(h, cfg["local_hosts"]))


def check_db(target, sess, cfg):
    v = Verdict("db", target)
    url = target
    if "://" not in target and re.match(r"^[A-Z_][A-Z0-9_]*$", target):
        url = os.environ.get(target, "")
        if not url:
            v.ev("environment variable %s is not set" % target)
            return v.done(UNKNOWN, "no database URL to check")
        v.ev("read from $%s" % target)
    v.target = _redact_url(url)
    if "://" not in url:
        if url.endswith((".db", ".sqlite", ".sqlite3")) or os.path.exists(url):
            url = "sqlite:///" + _real(url)
        else:
            v.ev("not a URL: %r" % url)
            return v.done(UNKNOWN, "cannot parse database target")
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    v.ev("scheme: %s" % scheme)
    if scheme.startswith("sqlite") or scheme == "file":
        path = unquote(parts.path)
        if parts.netloc and not path.startswith("/"):
            path = parts.netloc + "/" + path
        if path in ("", ":memory:", "/:memory:"):
            v.ev("in-memory database")
            return v.done(MINE, "in-memory database")
        pv = check_path(path, sess, cfg)
        v.evidence.extend(pv.evidence)
        nm = _classify_name([os.path.basename(path)], cfg, v)
        if nm and nm[0] == NOT_MINE:
            return v.done(*nm)
        return v.done(pv.verdict, "sqlite file: " + pv.reason)
    host = parts.hostname or ""
    dbname = unquote(parts.path.lstrip("/").split("/")[0]) if parts.path else ""
    if scheme.startswith("redis") and dbname.isdigit():
        dbname = ""
    user = unquote(parts.username or "")
    try:
        port = parts.port
    except ValueError:
        port = None
    v.ev("host: %s, port: %s, database: %s, user: %s" % (host or "(local socket)", port, dbname or "-", user or "-"))
    q = dict(x.split("=", 1) for x in parts.query.split("&") if "=" in x)
    if not host and q.get("host"):
        host = unquote(q["host"])
        v.ev("host from query string: %s" % host)
    hostkey = "%s:%s" % (host, port) if port else host
    res = _classify_name([host, dbname, user], cfg, v, "db_hosts", "db_hosts", hostkey)
    if res:
        return v.done(*res)
    if _is_local_host(host, cfg):
        v.ev("host is local")
        return v.done(MINE, "local database with no production markers")
    dev = _tokens_match(host, cfg["dev_patterns"]) or _tokens_match(dbname, cfg["dev_patterns"])
    if dev:
        v.ev("remote host, but name matches dev pattern %r" % dev)
    else:
        v.ev("remote host with no dev/local markers")
    return v.done(UNKNOWN, "remote database not allowlisted; cannot prove it is a dev copy")


# --- kube

def _kubeconfig_files():
    env = os.environ.get("KUBECONFIG")
    if env:
        return [p for p in env.split(os.pathsep) if p]
    return [os.path.join(os.path.expanduser("~"), ".kube", "config")]


def parse_kubeconfig(text):
    """Very small YAML subset parser for kubeconfig: current-context, contexts, clusters."""
    current = None
    contexts, clusters = {}, {}
    section, item = None, None

    def flush():
        if item is None:
            return
        if section == "contexts" and item.get("name"):
            contexts[item["name"]] = item
        elif section == "clusters" and item.get("name"):
            clusters[item["name"]] = item

    for raw in text.splitlines():
        line = raw.split(" #")[0].rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        s = line.strip()
        if indent == 0 and not s.startswith("-"):
            flush()
            item = None
            key, _, val = s.partition(":")
            val = val.strip().strip("\"'")
            if key == "current-context":
                current = val or None
                section = None
            elif key in ("contexts", "clusters"):
                section = key
            else:
                section = None
            continue
        if section is None:
            continue
        if s.startswith("- "):
            flush()
            item = {}
            s = s[2:].strip()
        if item is None:
            continue
        key, _, val = s.partition(":")
        val = val.strip().strip("\"'")
        if key in ("name", "cluster", "namespace", "user", "server") and val:
            item[key] = val
    flush()
    return current, contexts, clusters


def check_kube(target, sess, cfg, namespace=None):
    v = Verdict("kube", target)
    current, contexts, clusters = None, {}, {}
    for f in _kubeconfig_files():
        try:
            with open(os.path.expanduser(f)) as fh:
                c, ctx, cl = parse_kubeconfig(fh.read())
        except OSError:
            continue
        current = current or c
        for k, val in ctx.items():
            contexts.setdefault(k, val)
        for k, val in cl.items():
            clusters.setdefault(k, val)
        v.ev("kubeconfig: %s" % f)
    name = target
    if "/" in target and target not in contexts:
        name, namespace = target.rsplit("/", 1)
    if name in ("current", "", None):
        name = current
        v.ev("current-context: %s" % current)
    if not name:
        return v.done(UNKNOWN, "no kube context found")
    v.target = name + ("/" + namespace if namespace else "")
    ctx = contexts.get(name, {})
    cluster = ctx.get("cluster")
    server = clusters.get(cluster or "", {}).get("server")
    ns = namespace or ctx.get("namespace") or "default"
    v.ev("context: %s, cluster: %s, server: %s, namespace: %s" % (name, cluster, server, ns))
    if not ctx:
        v.ev("context %r is not defined in kubeconfig" % name)
    if sess and sess.get("kube_context_at_begin") and current and current != sess["kube_context_at_begin"]:
        v.ev("current-context changed during the session (was %s)" % sess["kube_context_at_begin"])
    if ns in SYSTEM_NAMESPACES:
        v.ev("namespace %s is a cluster system namespace" % ns)
        return v.done(NOT_MINE, "system namespace")
    host = urlsplit(server).hostname if server else None
    res = _classify_name([name, cluster, ns, host], cfg, v, "kube_contexts", "kube_contexts")
    if res:
        return v.done(*res)
    if name.lower().startswith(LOCAL_KUBE_PREFIXES) or (cluster or "").lower().startswith(LOCAL_KUBE_PREFIXES):
        v.ev("context name is a local cluster tool (kind/minikube/k3d/docker-desktop/...)")
        return v.done(MINE, "local development cluster")
    if host and _is_local_host(host, cfg):
        v.ev("API server %s is local" % host)
        return v.done(MINE, "local development cluster")
    return v.done(UNKNOWN, "remote cluster not allowlisted")


# --- aws / gcp

def _ini(path):
    cp = configparser.RawConfigParser()
    try:
        cp.read(os.path.expanduser(path))
    except configparser.Error:
        pass
    return cp


def check_aws(target, sess, cfg, live=False):
    v = Verdict("aws", target)
    profile = target
    if target in ("current", ""):
        profile = os.environ.get("AWS_PROFILE") or os.environ.get("AWS_DEFAULT_PROFILE") or "default"
        v.ev("active profile: %s (from %s)" % (profile, "AWS_PROFILE" if os.environ.get("AWS_PROFILE") else "default"))
    v.target = profile
    conf = _ini(os.environ.get("AWS_CONFIG_FILE", "~/.aws/config"))
    creds = _ini(os.environ.get("AWS_SHARED_CREDENTIALS_FILE", "~/.aws/credentials"))
    sec = "default" if profile == "default" else "profile " + profile
    data = dict(conf.items(sec)) if conf.has_section(sec) else {}
    if creds.has_section(profile):
        data.setdefault("_has_static_keys", "yes")
    if not data:
        v.ev("profile %r not found in AWS config/credentials" % profile)
    accounts = set()
    for key in ("sso_account_id", "account_id", "aws_account_id"):
        if data.get(key):
            accounts.add(data[key].strip())
    m = re.search(r"arn:aws[\w-]*:iam::(\d{12}):", data.get("role_arn", ""))
    if m:
        accounts.add(m.group(1))
    if data.get("role_arn"):
        v.ev("role_arn: %s" % data["role_arn"])
    endpoint = data.get("endpoint_url") or os.environ.get("AWS_ENDPOINT_URL")
    if live and shutil.which("aws"):
        rc, out, err = _run(["aws", "sts", "get-caller-identity", "--output", "json", "--profile", profile], timeout=20)
        if rc == 0:
            ident = json.loads(out)
            accounts.add(ident.get("Account", ""))
            v.ev("sts caller identity: %s" % ident.get("Arn"))
        else:
            v.ev("sts get-caller-identity failed: %s" % err.strip()[:160])
    for a in sorted(accounts):
        v.ev("account: %s" % a)
    for a in accounts:
        g = _glob_any(a, cfg["deny"].get("aws_accounts"))
        if g:
            v.ev("account %s is in deny.aws_accounts" % a)
            return v.done(NOT_MINE, "explicitly denied account")
    for a in accounts:
        if _glob_any(a, cfg["allow"].get("aws_accounts")):
            v.ev("account %s is in allow.aws_accounts" % a)
            return v.done(MINE, "explicitly allowed account")
    res = _classify_name([profile, data.get("role_arn"), data.get("sso_role_name"), data.get("sso_session")],
                         cfg, v, "aws_profiles", "aws_profiles")
    if res:
        return v.done(*res)
    if endpoint:
        host = urlsplit(endpoint).hostname
        v.ev("endpoint_url: %s" % endpoint)
        if _is_local_host(host, cfg):
            return v.done(MINE, "local AWS emulator (e.g. LocalStack)")
    return v.done(UNKNOWN, "AWS account not allowlisted")


def check_gcp(target, sess, cfg):
    v = Verdict("gcp", target)
    project = target
    gdir = os.environ.get("CLOUDSDK_CONFIG") or os.path.join(os.path.expanduser("~"), ".config", "gcloud")
    if target in ("current", ""):
        project = os.environ.get("CLOUDSDK_CORE_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
        src = "environment"
        if not project:
            active = "default"
            try:
                with open(os.path.join(gdir, "active_config")) as fh:
                    active = fh.read().strip() or "default"
            except OSError:
                pass
            cp = _ini(os.path.join(gdir, "configurations", "config_" + active))
            if cp.has_option("core", "project"):
                project = cp.get("core", "project")
            src = "gcloud configuration %r" % active
        v.ev("active project: %s (from %s)" % (project, src))
    if not project:
        return v.done(UNKNOWN, "no GCP project configured")
    v.target = project
    res = _classify_name([project], cfg, v, "gcp_projects", "gcp_projects")
    if res:
        return v.done(*res)
    return v.done(UNKNOWN, "GCP project not allowlisted")


# --- git force-push

def check_git(target, sess, cfg, repo=None):
    v = Verdict("git", target)
    repo = repo or os.getcwd()
    remote, branch = "origin", target
    rc, out, _ = _run(["git", "remote"], cwd=repo)
    remotes = out.split()
    if "/" in target and target.split("/", 1)[0] in remotes:
        remote, branch = target.split("/", 1)
    if branch in ("current", "HEAD", ""):
        rc, out, _ = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
        branch = out.strip()
    v.target = "%s/%s" % (remote, branch)
    rc, url, _ = _run(["git", "remote", "get-url", remote], cwd=repo)
    if rc != 0:
        v.ev("remote %r not found in %s" % (remote, repo))
        return v.done(UNKNOWN, "unknown remote")
    v.ev("remote %s -> %s" % (remote, _redact_url(url.strip())))
    for pat in cfg["protected_branches"]:
        if fnmatch.fnmatchcase(branch, pat):
            v.ev("branch %r matches protected pattern %r" % (branch, pat))
            return v.done(NOT_MINE, "force-pushing a protected/shared branch")
    me_email = _run(["git", "config", "user.email"], cwd=repo)[1].strip().lower()
    rc, _, _ = _run(["git", "rev-parse", "--verify", "-q", "refs/remotes/%s/%s" % (remote, branch)], cwd=repo)
    foreign, lost = [], 0
    if rc == 0:
        rc, out, _ = _run(["git", "log", "--format=%H%x09%ae%x09%ct", "%s..%s/%s" % (branch, remote, branch)], cwd=repo)
        for line in out.splitlines():
            h, email, ts = line.split("\t")
            lost += 1
            if email.lower() != me_email:
                foreign.append((h[:9], email))
        v.ev("%d commit(s) on %s/%s are not in local %s and would be overwritten" % (lost, remote, branch, branch))
    else:
        v.ev("no remote-tracking ref %s/%s (fetch first for a full check)" % (remote, branch))
    if foreign:
        v.ev("commits by others that would be lost: %s" % ", ".join("%s (%s)" % f for f in foreign[:5]))
        return v.done(NOT_MINE, "force-push would delete other people's commits")
    created = None
    rc, out, _ = _run(["git", "reflog", "show", "--date=unix", "--format=%gd %gs", "refs/heads/" + branch], cwd=repo)
    lines = [x for x in out.splitlines() if x.strip()]
    if lines:
        m = re.search(r"@\{(\d+)\}", lines[-1])
        if m:
            created = int(m.group(1))
            v.ev("branch first appears in reflog at %s (%s)" % (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created)), lines[-1].split(" ", 1)[-1][:60]))
    if sess and created is not None and created >= sess["start"] - 1:
        return v.done(MINE, "branch created during this session; no foreign commits would be lost")
    for r in (sess or {}).get("registered", {}).get("git", []):
        if r["branch"] == branch:
            v.ev("branch registered by this session")
            return v.done(MINE, "registered by this session")
    if not sess:
        v.ev("no session to compare branch age against")
    return v.done(UNKNOWN, "branch predates the session; ask before rewriting its history")


# --- docker

def _docker_bin():
    return os.environ.get("NOTMINE_DOCKER") or shutil.which("docker")


def check_docker(target, sess, cfg):
    v = Verdict("docker", target)
    dk = _docker_bin()
    if not dk:
        v.ev("docker CLI not found")
        return v.done(UNKNOWN, "cannot inspect container")
    dh = os.environ.get("DOCKER_HOST", "")
    if dh and not dh.startswith("unix://") and not _is_local_host(urlsplit(dh).hostname, cfg):
        v.ev("DOCKER_HOST points at a remote engine: %s" % dh)
        return v.done(NOT_MINE, "remote Docker engine")
    rc, out, err = _run([dk, "inspect", "--type", "container", target])
    if rc != 0:
        v.ev("docker inspect failed: %s" % (err.strip()[:160] or out.strip()[:160]))
        return v.done(UNKNOWN, "container not found")
    try:
        c = json.loads(out)[0]
    except (ValueError, IndexError):
        return v.done(UNKNOWN, "unreadable docker inspect output")
    name = (c.get("Name") or "").lstrip("/")
    image = (c.get("Config") or {}).get("Image", "")
    labels = (c.get("Config") or {}).get("Labels") or {}
    created_s = c.get("Created", "")
    v.ev("name: %s, image: %s, id: %s" % (name, image, (c.get("Id") or "")[:12]))
    created = None
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)", created_s)
    if m:
        import calendar
        created = calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))
        v.ev("created: %s UTC" % m.group(1))
    wd = labels.get("com.docker.compose.project.working_dir")
    if wd:
        v.ev("compose working_dir: %s (project %s)" % (wd, labels.get("com.docker.compose.project")))
    for r in (sess or {}).get("registered", {}).get("docker", []):
        if r["name"] in (name, target) or (c.get("Id") or "").startswith(r["name"]):
            v.ev("registered by this session")
            return v.done(MINE, "registered by this session")
    res = _classify_name([name, image, labels.get("com.docker.compose.project"),
                          labels.get("environment"), labels.get("env")], cfg, v, "docker", "docker")
    if res:
        return v.done(*res)
    if not sess:
        return v.done(UNKNOWN, "no session registry; ownership cannot be proven")
    if created is not None and created < sess["start"] - 1:
        v.ev("container was created before the session began")
        return v.done(NOT_MINE, "container predates this session")
    if wd and _inside(wd, sess["workspace"]):
        return v.done(MINE, "compose project in this workspace, created during the session")
    if created is not None:
        v.ev("created during the session, but not tied to the workspace")
    return v.done(UNKNOWN, "cannot tie container to this workspace")


# --------------------------------------------------------------------------- commands

def cmd_begin(args):
    ws = _real(args.workspace or os.getcwd())
    path = _session_path(ws)
    if os.path.isfile(path) and not args.force:
        with open(path) as fh:
            old = json.load(fh)
        print("notmine: session already active for %s (started %s). Use --force to restart." % (
            ws, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(old["start"]))))
        return 0
    me = os.getpid()
    chain = ancestors(me)
    root = None
    if args.root_pid or os.environ.get("NOTMINE_ROOT_PID"):
        rp = int(args.root_pid or os.environ["NOTMINE_ROOT_PID"])
        info = proc_info(rp)
        if not info:
            raise SystemExit("notmine: root pid %d not running" % rp)
        root = {"pid": rp, "start": info["start"], "cmd": info["cmd"][:200]}
    elif not args.no_auto_root:
        found = _detect_agent_root(chain)
        if found:
            root = {"pid": found["pid"], "start": found["start"], "cmd": found["cmd"][:200]}
    start = _now()
    pre = {str(k): val for k, val in all_pids().items()}
    ports = {}
    for p in args.watch_ports or []:
        ls = listeners(p)
        if ls:
            ports[str(p)] = [x["pid"] for x in ls]
    kctx = None
    for f in _kubeconfig_files():
        try:
            with open(os.path.expanduser(f)) as fh:
                kctx = parse_kubeconfig(fh.read())[0]
            if kctx:
                break
        except OSError:
            pass
    sess = {"version": 1, "id": hashlib.sha1(("%s%f" % (ws, start)).encode()).hexdigest()[:12],
            "workspace": ws, "start": start, "host": socket.gethostname(), "uid": os.getuid(),
            "begin_pid": me,
            "ancestors": [{"pid": a["pid"], "start": a["start"], "cmd": a["cmd"][:200]} for a in chain],
            "root": root, "preexisting": pre, "ports_at_begin": ports,
            "kube_context_at_begin": kctx, "aws_profile_at_begin": os.environ.get("AWS_PROFILE"),
            "registered": {}}
    save_session(sess, path)
    print("notmine: session %s started for %s" % (sess["id"], ws))
    print("  %d pre-existing processes recorded%s" % (
        len(pre), "; agent root: pid %d (%s)" % (root["pid"], root["cmd"][:40]) if root else "; no agent root detected"))
    print("  state: %s" % path)
    return 0


def cmd_end(args):
    sess, path = find_session()
    if not sess:
        print("notmine: no active session here")
        return 0
    os.remove(path)
    print("notmine: session %s for %s ended" % (sess["id"], sess["workspace"]))
    return 0


def cmd_status(args):
    sess, path = find_session()
    if not sess:
        print("notmine: no active session covers %s" % os.getcwd())
        return 11
    out = {k: sess[k] for k in ("id", "workspace", "start", "root", "registered", "kube_context_at_begin")}
    out["state_file"] = path
    out["preexisting_processes"] = len(sess.get("preexisting", {}))
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print("session %s  workspace %s" % (sess["id"], sess["workspace"]))
        print("started %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sess["start"])))
        print("root    %s" % (sess["root"] or "none"))
        print("registered %s" % json.dumps(sess.get("registered", {})))
    return 0


def cmd_register(args):
    sess, path = find_session()
    if not sess:
        raise SystemExit("notmine: no session; run `notmine begin` first")
    reg = sess.setdefault("registered", {})
    k, t = args.kind, args.target
    if k == "pid":
        info = proc_info(int(t))
        if not info:
            raise SystemExit("notmine: pid %s not running" % t)
        entry = {"pid": int(t), "start": info["start"], "cmd": info["cmd"][:200]}
    elif k == "port":
        entry = {"port": int(t.lstrip(":"))}
        for o in listeners(entry["port"]):
            if o["pid"]:
                info = proc_info(o["pid"])
                if info:
                    reg.setdefault("pid", []).append({"pid": o["pid"], "start": info["start"], "cmd": info["cmd"][:200]})
    elif k == "path":
        entry = {"path": _real(t)}
    elif k == "git":
        entry = {"branch": t}
    elif k == "docker":
        entry = {"name": t}
    reg.setdefault(k, []).append(entry)
    save_session(sess, path)
    print("notmine: registered %s %s as mine" % (k, t))
    return 0


def run_check(kind, target, args):
    sess, _ = find_session()
    cfg = load_config(sess["workspace"] if sess else None)
    if kind == "pid":
        return check_pid(target, sess, cfg)
    if kind == "port":
        return check_port(target, sess, cfg)
    if kind == "path":
        return check_path(target, sess, cfg)
    if kind == "db":
        return check_db(target, sess, cfg)
    if kind == "kube":
        return check_kube(target, sess, cfg, getattr(args, "namespace", None))
    if kind == "aws":
        return check_aws(target, sess, cfg, getattr(args, "live", False))
    if kind == "gcp":
        return check_gcp(target, sess, cfg)
    if kind == "git":
        return check_git(target, sess, cfg, getattr(args, "repo", None))
    if kind == "docker":
        return check_docker(target, sess, cfg)
    raise SystemExit("notmine: unknown kind %s" % kind)


def print_verdict(v, as_json):
    if as_json:
        print(json.dumps(v.to_dict(), indent=2))
        return
    mark = {MINE: "MINE", NOT_MINE: "NOT MINE", UNKNOWN: "UNKNOWN"}[v.verdict]
    print("%s  %s %s: %s" % (mark, v.kind, v.target, v.reason))
    for e in v.evidence:
        print("  - " + e)
    if v.verdict != MINE:
        print("  => stop and ask the user before acting on this target")


def cmd_check(args):
    v = run_check(args.kind, args.target, args)
    print_verdict(v, args.json)
    return EXIT[v.verdict]


def cmd_kill(args):
    sig = getattr(signal, "SIG" + args.signal.upper().replace("SIG", ""), None)
    if sig is None:
        raise SystemExit("notmine: unknown signal %s" % args.signal)
    t = args.target
    kind = "port" if t.startswith(":") else "pid"
    v = run_check(kind, t.lstrip(":") if kind == "port" else t, args)
    print_verdict(v, args.json)
    if v.verdict != MINE:
        return EXIT[v.verdict]
    sess, _ = find_session()
    cfg = load_config(sess["workspace"] if sess else None)
    pids = [int(t)] if kind == "pid" else [o["pid"] for o in listeners(int(t.lstrip(":"))) if o["pid"]]
    for pid in pids:
        recheck = check_pid(pid, sess, cfg)  # re-verify right before signalling (PID reuse)
        if recheck.verdict != MINE:
            print("notmine: pid %d no longer verifies as mine; not signalling" % pid)
            return EXIT[recheck.verdict]
        if args.dry_run:
            print("notmine: would send %s to pid %d" % (args.signal.upper(), pid))
            continue
        try:
            os.kill(pid, sig)
            print("notmine: sent %s to pid %d" % (args.signal.upper(), pid))
        except OSError as exc:
            print("notmine: kill %d failed: %s" % (pid, exc))
            return 1
    return 0


KINDS = ["pid", "port", "path", "db", "kube", "aws", "gcp", "git", "docker"]


def build_parser():
    p = argparse.ArgumentParser(prog="notmine", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="exit codes: 0 MINE, 10 NOT_MINE, 11 UNKNOWN, 2 usage error")
    p.add_argument("--version", action="version", version="notmine " + __version__)
    sub = p.add_subparsers(dest="cmd")
    b = sub.add_parser("begin", help="record session start, workspace and pre-existing processes")
    b.add_argument("--workspace", help="task workspace (default: cwd)")
    b.add_argument("--root-pid", type=int, help="pid of the agent process; its descendants count as mine")
    b.add_argument("--no-auto-root", action="store_true", help="do not auto-detect the agent process")
    b.add_argument("--watch-ports", type=int, nargs="*", default=[3000, 5000, 5173, 8000, 8080, 8888],
                   help="ports whose current owners are recorded as pre-existing")
    b.add_argument("--force", action="store_true", help="restart an existing session")
    b.set_defaults(func=cmd_begin)
    e = sub.add_parser("end", help="forget the session for this workspace")
    e.set_defaults(func=cmd_end)
    s = sub.add_parser("status", help="show the active session")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)
    r = sub.add_parser("register", help="declare a target you created as mine")
    r.add_argument("kind", choices=["pid", "port", "path", "git", "docker"])
    r.add_argument("target")
    r.set_defaults(func=cmd_register)
    c = sub.add_parser("check", help="verdict on whether a target belongs to this task")
    c.add_argument("kind", choices=KINDS)
    c.add_argument("target", nargs="?", default="current",
                   help="pid | port | path | DB URL or env var name | kube context[/ns] | aws profile | "
                        "gcp project | [remote/]branch | container  (default: current)")
    c.add_argument("--json", action="store_true")
    c.add_argument("--namespace", "-n", help="kube namespace")
    c.add_argument("--live", action="store_true", help="aws: also call `aws sts get-caller-identity`")
    c.add_argument("--repo", help="git: repository path (default: cwd)")
    c.set_defaults(func=cmd_check)
    k = sub.add_parser("kill", help="signal a pid or :port only if it verifies as mine")
    k.add_argument("target", help="pid or :port")
    k.add_argument("--signal", default="TERM")
    k.add_argument("--dry-run", action="store_true")
    k.add_argument("--json", action="store_true")
    k.set_defaults(func=cmd_kill)
    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
