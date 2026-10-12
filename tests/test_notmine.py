"""Unit and end-to-end tests. Stdlib unittest only: python3 -m unittest discover -s tests -v"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "bench"))

import notmine as nm  # noqa: E402

CLI = os.path.join(ROOT, "scripts", "notmine.py")


def cfg(**over):
    c = nm.load_config(None)
    for k, v in over.items():
        c[k] = v
    return c


class Env(unittest.TestCase):
    """Isolated HOME / state dir per test."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="nm-test-"))
        self.ws = os.path.join(self.tmp, "ws")
        os.makedirs(self.ws)
        self.old_env = dict(os.environ)
        os.environ.update({"NOTMINE_HOME": os.path.join(self.tmp, "state"), "HOME": os.path.join(self.tmp, "home"),
                           "XDG_CONFIG_HOME": os.path.join(self.tmp, "home", ".config")})
        os.makedirs(os.environ["HOME"])
        for k in ("NOTMINE_SESSION_FILE", "NOTMINE_CONFIG", "KUBECONFIG", "AWS_PROFILE", "DOCKER_HOST",
                  "CLOUDSDK_CORE_PROJECT", "GOOGLE_CLOUD_PROJECT", "NOTMINE_ROOT_PID"):
            os.environ.pop(k, None)
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            p.kill()
            p.wait()
        os.environ.clear()
        os.environ.update(self.old_env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args, cwd=None):
        return subprocess.run([sys.executable, CLI] + list(args), cwd=cwd or self.ws,
                              capture_output=True, text=True, timeout=60)

    def check(self, *args, cwd=None):
        r = self.cli("check", *(list(args) + ["--json"]), cwd=cwd)
        self.assertIn(r.returncode, (0, 10, 11), r.stderr + r.stdout)
        d = json.loads(r.stdout)
        self.assertEqual(r.returncode, nm.EXIT[d["verdict"]])
        return d

    def begin(self):
        r = self.cli("begin", "--no-auto-root", "--force", "--watch-ports")
        self.assertEqual(r.returncode, 0, r.stderr)

    def spawn(self, cmd, cwd):
        p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        return p


class TestPatterns(unittest.TestCase):
    def test_token_boundaries(self):
        P = nm.DEFAULT_PROD
        for yes in ("prod", "prod-db.internal", "db_prod", "app.prod.internal", "PRODUCTION", "shop_live", "prd-eu"):
            self.assertTrue(nm._tokens_match(yes, P), yes)
        for no in ("product", "products-db", "preprod", "deliver", "olive", "reproduce", ""):
            self.assertFalse(nm._tokens_match(no, P), no)

    def test_redact(self):
        self.assertEqual(nm._redact_url("postgres://u:s3cret@h/db"), "postgres://u:***@h/db")
        self.assertEqual(nm._redact_url("postgres://h/db"), "postgres://h/db")


class TestDB(unittest.TestCase):
    def v(self, url, c=None):
        return nm.check_db(url, None, c or cfg()).verdict

    def test_local_dev(self):
        self.assertEqual(self.v("postgres://app:pw@localhost:5432/app_dev"), nm.MINE)
        self.assertEqual(self.v("redis://127.0.0.1:6379/0"), nm.MINE)
        self.assertEqual(self.v("postgresql:///app?host=/var/run/postgresql"), nm.MINE)
        self.assertEqual(self.v("postgresql:///app"), nm.MINE)
        self.assertEqual(self.v("sqlite:///:memory:"), nm.MINE)

    def test_prod(self):
        self.assertEqual(self.v("postgres://a@prod-db.internal/app"), nm.NOT_MINE)
        self.assertEqual(self.v("mysql://a@db.example.com/shop_production"), nm.NOT_MINE)
        self.assertEqual(self.v("postgres://a@localhost/app_prod_copy"), nm.NOT_MINE)
        self.assertEqual(self.v("postgres://prod_admin@localhost/app"), nm.NOT_MINE)

    def test_remote_and_shared(self):
        self.assertEqual(self.v("postgres://a@x.rds.amazonaws.com/app"), nm.UNKNOWN)
        self.assertEqual(self.v("postgres://a@staging-db/app"), nm.UNKNOWN)
        self.assertEqual(self.v("postgres://a@dev-db.example.com/app"), nm.UNKNOWN)

    def test_allow_deny(self):
        c = cfg()
        c["allow"] = {"db_hosts": ["dev-db.example.com"]}
        c["deny"] = {"db_hosts": ["localhost:5433"]}
        self.assertEqual(self.v("postgres://a@dev-db.example.com/app", c), nm.MINE)
        self.assertEqual(self.v("postgres://a@localhost:5433/app", c), nm.NOT_MINE)

    def test_env_var(self):
        os.environ["NM_TEST_DB"] = "postgres://u:pw@prod.example.com/x"
        try:
            v = nm.check_db("NM_TEST_DB", None, cfg())
            self.assertEqual(v.verdict, nm.NOT_MINE)
            self.assertNotIn("pw", v.target)
        finally:
            del os.environ["NM_TEST_DB"]


class TestKubeParse(unittest.TestCase):
    TEXT = """apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: AAA
    server: https://127.0.0.1:6443
  name: kind-x
- name: prod
  cluster:
    server: https://k8s.example.com
contexts:
- context:
    cluster: kind-x
    namespace: app
    user: kind-x
  name: kind-x
- name: prod
  context:
    cluster: prod
    user: admin
current-context: prod
kind: Config
users:
- name: admin
  user:
    token: secret
"""

    def test_parse(self):
        cur, ctx, cl = nm.parse_kubeconfig(self.TEXT)
        self.assertEqual(cur, "prod")
        self.assertEqual(ctx["kind-x"]["namespace"], "app")
        self.assertEqual(ctx["prod"]["cluster"], "prod")
        self.assertEqual(cl["prod"]["server"], "https://k8s.example.com")
        self.assertNotIn("admin", cl)


class TestKubeAwsGcp(Env):
    def test_kube(self):
        path = os.path.join(self.tmp, "kc")
        with open(path, "w") as fh:
            fh.write(TestKubeParse.TEXT)
        os.environ["KUBECONFIG"] = path
        self.assertEqual(nm.check_kube("current", None, cfg()).verdict, nm.NOT_MINE)
        self.assertEqual(nm.check_kube("kind-x", None, cfg()).verdict, nm.MINE)
        self.assertEqual(nm.check_kube("kind-x", None, cfg(), "kube-system").verdict, nm.NOT_MINE)
        self.assertEqual(nm.check_kube("nope", None, cfg()).verdict, nm.UNKNOWN)

    def test_aws(self):
        conf = os.path.join(self.tmp, "awsconf")
        with open(conf, "w") as fh:
            fh.write("[default]\nregion=x\n[profile p1]\nrole_arn=arn:aws:iam::123456789012:role/R\n"
                     "[profile local]\nendpoint_url=http://localhost:4566\n[profile prod-ro]\nregion=x\n")
        os.environ["AWS_CONFIG_FILE"] = conf
        os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.path.join(self.tmp, "nocreds")
        c = cfg()
        self.assertEqual(nm.check_aws("p1", None, c).verdict, nm.UNKNOWN)
        c["allow"] = {"aws_accounts": ["123456789012"]}
        self.assertEqual(nm.check_aws("p1", None, c).verdict, nm.MINE)
        c["deny"] = {"aws_accounts": ["123456789012"]}
        self.assertEqual(nm.check_aws("p1", None, c).verdict, nm.NOT_MINE)
        self.assertEqual(nm.check_aws("local", None, cfg()).verdict, nm.MINE)
        self.assertEqual(nm.check_aws("prod-ro", None, cfg()).verdict, nm.NOT_MINE)
        os.environ["AWS_PROFILE"] = "prod-ro"
        self.assertEqual(nm.check_aws("current", None, cfg()).verdict, nm.NOT_MINE)

    def test_gcp(self):
        os.environ["CLOUDSDK_CONFIG"] = os.path.join(self.tmp, "gc")
        os.makedirs(os.path.join(self.tmp, "gc", "configurations"))
        with open(os.path.join(self.tmp, "gc", "configurations", "config_default"), "w") as fh:
            fh.write("[core]\nproject = acme-production\n")
        self.assertEqual(nm.check_gcp("current", None, cfg()).verdict, nm.NOT_MINE)
        os.environ["GOOGLE_CLOUD_PROJECT"] = "my-dev-1"
        self.assertEqual(nm.check_gcp("current", None, cfg()).verdict, nm.UNKNOWN)
        c = cfg()
        c["allow"] = {"gcp_projects": ["my-dev-*"]}
        self.assertEqual(nm.check_gcp("current", None, c).verdict, nm.MINE)


class TestSessionAndPaths(Env):
    def test_no_session_process_unknown(self):
        p = self.spawn(["sleep", "30"], self.ws)
        time.sleep(0.2)
        self.assertEqual(self.check("pid", str(p.pid))["verdict"], nm.UNKNOWN)

    def test_status_and_end(self):
        self.assertEqual(self.cli("status").returncode, 11)
        self.begin()
        self.assertEqual(self.cli("status").returncode, 0)
        sub = os.path.join(self.ws, "a", "b")
        os.makedirs(sub)
        self.assertEqual(self.cli("status", cwd=sub).returncode, 0, "session found from subdirectory")
        self.assertEqual(self.cli("end").returncode, 0)
        self.assertEqual(self.cli("status").returncode, 11)

    def test_paths(self):
        self.begin()
        self.assertEqual(self.check("path", "build")["verdict"], nm.MINE)
        self.assertEqual(self.check("path", ".")["verdict"], nm.UNKNOWN)
        self.assertEqual(self.check("path", "..")["verdict"], nm.UNKNOWN)
        self.assertEqual(self.check("path", "/")["verdict"], nm.NOT_MINE)
        self.assertEqual(self.check("path", os.environ["HOME"])["verdict"], nm.NOT_MINE)
        self.assertEqual(self.check("path", "/usr/bin")["verdict"], nm.NOT_MINE)
        self.assertEqual(self.check("path", ".git/config")["verdict"], nm.UNKNOWN)
        os.symlink("/etc", os.path.join(self.ws, "etc-link"))
        self.assertEqual(self.check("path", "etc-link")["verdict"], nm.NOT_MINE)
        self.assertEqual(self.check("path", "etc-link/hosts")["verdict"], nm.NOT_MINE)

    def test_protected_paths_config(self):
        with open(os.path.join(self.ws, ".notmine.json"), "w") as fh:
            json.dump({"protected_paths": ["uploads", "*.env"]}, fh)
        self.begin()
        self.assertEqual(self.check("path", "uploads/a.jpg")["verdict"], nm.NOT_MINE)
        self.assertEqual(self.check("path", "prod.env")["verdict"], nm.NOT_MINE)
        self.assertEqual(self.check("path", "src/x.py")["verdict"], nm.MINE)

    def test_temp(self):
        old = tempfile.mkdtemp(prefix="nm-old-")
        time.sleep(1.2)
        self.begin()
        new = tempfile.mkdtemp(prefix="nm-new-")
        try:
            self.assertEqual(self.check("path", new)["verdict"], nm.MINE)
            self.assertEqual(self.check("path", old)["verdict"], nm.NOT_MINE)
        finally:
            shutil.rmtree(old)
            shutil.rmtree(new)


class TestProcesses(Env):
    def test_lifecycle(self):
        before = self.spawn(["sleep", "60"], self.ws)
        time.sleep(1.2)
        self.begin()
        after = self.spawn(["sleep", "60"], self.ws)
        elsewhere = self.spawn(["sleep", "60"], self.tmp)
        time.sleep(0.3)
        self.assertEqual(self.check("pid", str(before.pid))["verdict"], nm.NOT_MINE)
        self.assertEqual(self.check("pid", str(after.pid))["verdict"], nm.MINE)
        self.assertEqual(self.check("pid", str(elsewhere.pid))["verdict"], nm.UNKNOWN)
        self.assertEqual(self.cli("register", "pid", str(elsewhere.pid)).returncode, 0)
        self.assertEqual(self.check("pid", str(elsewhere.pid))["verdict"], nm.MINE)
        self.assertEqual(self.check("pid", str(os.getpid()))["verdict"], nm.NOT_MINE, "self/ancestor")
        self.assertEqual(self.check("pid", "1")["verdict"], nm.NOT_MINE)
        self.assertEqual(self.check("pid", "999999")["verdict"], nm.UNKNOWN)

    def test_root_pid_descendants(self):
        root = self.spawn(["sh", "-c", "sleep 1.5; sleep 60 & wait"], self.tmp)
        time.sleep(0.2)
        r = self.cli("begin", "--force", "--root-pid", str(root.pid), "--watch-ports")
        self.assertEqual(r.returncode, 0, r.stderr)
        time.sleep(2.0)
        out = subprocess.run(["ps", "-o", "pid=,ppid=,command=", "-A"], capture_output=True, text=True).stdout
        kids = [int(l.split()[0]) for l in out.splitlines()
                if len(l.split()) > 2 and l.split()[1] == str(root.pid) and "sleep 60" in l]
        self.assertTrue(kids, out)
        d = self.check("pid", str(kids[0]))
        self.assertEqual(d["verdict"], nm.MINE, d)

    def test_pid_reuse_guard(self):
        self.begin()
        p = self.spawn(["sleep", "60"], self.tmp)
        time.sleep(0.2)
        self.assertEqual(self.cli("register", "pid", str(p.pid)).returncode, 0)
        sess, path = nm.find_session(self.ws)
        sess["registered"]["pid"][0]["start"] -= 100  # pretend the registered pid was a different process
        nm.save_session(sess, path)
        d = self.check("pid", str(p.pid))
        self.assertNotEqual(d["verdict"], nm.MINE)
        self.assertTrue(any("reused" in e for e in d["evidence"]))

    def test_ports_and_kill(self):
        def free():
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            s.close()
            return port

        def wait(port):
            for _ in range(100):
                s = socket.socket()
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    s.close()
                    return
                s.close()
                time.sleep(0.1)
            self.fail("server did not start")

        other_dir = os.path.join(self.tmp, "prod")
        os.makedirs(other_dir)
        pport, mport = free(), free()
        prod = self.spawn([sys.executable, "-m", "http.server", str(pport), "--bind", "127.0.0.1"], other_dir)
        wait(pport)
        time.sleep(1.2)
        self.begin()
        mine = self.spawn([sys.executable, "-m", "http.server", str(mport), "--bind", "127.0.0.1"], self.ws)
        wait(mport)
        self.assertEqual(self.check("port", str(pport))["verdict"], nm.NOT_MINE)
        self.assertEqual(self.check("port", str(mport))["verdict"], nm.MINE)
        self.assertEqual(self.check("port", "1")["verdict"], nm.UNKNOWN)
        r = self.cli("kill", ":%d" % pport)
        self.assertEqual(r.returncode, 10)
        self.assertIsNone(prod.poll(), "prod server must survive")
        r = self.cli("kill", ":%d" % mport, "--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIsNone(mine.poll())
        r = self.cli("kill", ":%d" % mport)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        mine.wait(timeout=5)
        self.assertIsNotNone(mine.poll())
        self.assertIsNone(prod.poll())


class TestGit(Env):
    def git(self, *a, cwd=None, email="me@example.com"):
        env = dict(os.environ, GIT_AUTHOR_NAME="x", GIT_COMMITTER_NAME="x", GIT_AUTHOR_EMAIL=email,
                   GIT_COMMITTER_EMAIL=email, GIT_CONFIG_GLOBAL=os.path.join(self.tmp, "gitconfig"))
        subprocess.run(["git"] + list(a), cwd=cwd or self.ws, check=True, capture_output=True, env=env)

    def test_force_push(self):
        remote = os.path.join(self.tmp, "r.git")
        self.git("init", "--bare", "-b", "main", remote, cwd=self.tmp)
        self.git("init", "-b", "main")
        self.git("config", "user.email", "me@example.com")
        self.git("commit", "--allow-empty", "-m", "a")
        self.git("remote", "add", "origin", remote)
        self.git("push", "-u", "origin", "main")
        self.git("checkout", "-b", "old")
        self.git("push", "-u", "origin", "old")
        other = os.path.join(self.tmp, "other")
        self.git("clone", "-b", "old", remote, other, cwd=self.tmp)
        self.git("commit", "--allow-empty", "-m", "theirs", cwd=other, email="bob@example.com")
        self.git("push", "origin", "old", cwd=other, email="bob@example.com")
        self.git("fetch")
        time.sleep(1.1)
        self.begin()
        self.git("checkout", "-b", "agent/fix")
        self.assertEqual(self.check("git", "main")["verdict"], nm.NOT_MINE)
        self.assertEqual(self.check("git", "origin/old")["verdict"], nm.NOT_MINE)
        self.assertEqual(self.check("git", "agent/fix")["verdict"], nm.MINE)
        self.assertEqual(self.check("git", "current")["verdict"], nm.MINE)


class TestDocker(Env):
    def test_docker_shim(self):
        self.begin()
        sess, _ = nm.find_session(self.ws)
        iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S.0Z", time.gmtime(t))
        data = {
            "mine": {"Name": "/mine", "Id": "1" * 64, "Created": iso(sess["start"] + 5),
                     "Config": {"Image": "app:dev", "Labels": {"com.docker.compose.project.working_dir": self.ws}}},
            "old": {"Name": "/old", "Id": "2" * 64, "Created": iso(sess["start"] - 999), "Config": {"Image": "x"}},
            "prodish": {"Name": "/api-prod", "Id": "3" * 64, "Created": iso(sess["start"] + 5), "Config": {"Image": "x"}},
        }
        dj = os.path.join(self.tmp, "d.json")
        with open(dj, "w") as fh:
            json.dump(data, fh)
        shim = os.path.join(self.tmp, "docker")
        with open(shim, "w") as fh:
            fh.write("#!%s\nimport json,sys\nd=json.load(open(%r))\nc=d.get(sys.argv[-1])\n"
                     "sys.exit(1) if c is None else print(json.dumps([c]))\n" % (sys.executable, dj))
        os.chmod(shim, 0o755)
        os.environ["NOTMINE_DOCKER"] = shim
        self.assertEqual(self.check("docker", "mine")["verdict"], nm.MINE)
        self.assertEqual(self.check("docker", "old")["verdict"], nm.NOT_MINE)
        self.assertEqual(self.check("docker", "prodish")["verdict"], nm.NOT_MINE)
        self.assertEqual(self.check("docker", "missing")["verdict"], nm.UNKNOWN)
        os.environ["DOCKER_HOST"] = "tcp://10.0.0.5:2376"
        self.assertEqual(self.check("docker", "mine")["verdict"], nm.NOT_MINE)


class TestBench(unittest.TestCase):
    def test_bench_has_no_unsafe_passes(self):
        import run_bench
        results, s = run_bench.run_bench()
        misses = [(r["label"], r["got"], r["reason"]) for r in results
                  if not r.get("hard") and r["got"] != r["expected"]]
        self.assertEqual(s["unsafe_passes"], 0, misses)
        self.assertEqual(s["exit_code_mismatches"], 0)
        self.assertEqual(misses, [])


if __name__ == "__main__":
    unittest.main()
