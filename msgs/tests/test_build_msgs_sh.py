"""build-msgs.sh, host side, against a `docker` stub that records argv and captures the
`local-sources` named context before the script's trap deletes it. The default (FLEET_MSGS_LOCAL
unset) argv carries no named context; with it set, each checkout is snapshotted the way git sees
it (uncommitted edits + untracked non-ignored files; no .git, no ignored trees, no deleted files)
through a throwaway index — the checkout's own index and working tree are untouched — and
sources.tsv carries the identity the host saw (tag/SHA, -dirty, HEAD or unknown). The named context
rides both rig-build.sh paths (docker build, and buildx with --platform/--push/--load). Real git,
no daemon. Run: python3 msgs/tests/test_build_msgs_sh.py"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "build-msgs.sh"

DOCKER_STUB = f"""#!{sys.executable}
import json, os, shutil, sys
a = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps(a) + "\\n")
for v in a:
    if v.startswith("local-sources="):
        shutil.copytree(v.split("=", 1)[1], os.environ["STUB_CAPTURE"], symlinks=True)
if "--metadata-file" in a:
    with open(a[a.index("--metadata-file") + 1], "w") as f:
        json.dump({{"containerimage.digest": "sha256:" + "0" * 64}}, f)
"""

MANIFEST = """source:
  - repo: https://github.com/acme/fleet_msgs.git
    ref: v1.0.0
    packages: [a_msgs]
"""

# Determinism: the developer's global git config (excludes, signing, hooks) must not leak in.
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


class BuildMsgsSh(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name).resolve()
        (self.tmp / "bin").mkdir()
        (self.tmp / "bin" / "docker").write_text(DOCKER_STUB)
        (self.tmp / "bin" / "docker").chmod(0o755)
        (self.tmp / "tmp").mkdir()
        self.log = self.tmp / "docker.jsonl"
        self.capture = self.tmp / "captured"
        self.manifest = self.tmp / "msgs.yaml"
        self.manifest.write_text(MANIFEST)
        self.repo = self.tmp / "fleet_msgs"
        self.repo.mkdir()
        self.git("init", "-q")
        self.write("src/a_msgs/msg/A.msg", "uint8 a\n")
        self.write("src/a_msgs/CMakeLists.txt", "msg/A.msg\n")
        self.write("gone.txt", "deleted in the working tree, never committed as deleted\n")
        self.write(".gitignore", "build/\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")
        self.git("tag", "v1.0.0")
        self.head = self.git("rev-parse", "HEAD")

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-C", str(cwd or self.repo), *args], check=True, text=True,
                              capture_output=True, env={**os.environ, **GIT_ENV}).stdout.strip()

    def write(self, rel, text, root=None):
        path = (root or self.repo) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_script(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("RIG_", "FLEET_", "PUSH", "SSH_AUTH_SOCK"))}
        env.update(GIT_ENV, PATH=f"{self.tmp / 'bin'}:{env.get('PATH', '')}", HOME=str(self.tmp),
                   TMPDIR=str(self.tmp / "tmp"), STUB_LOG=str(self.log),
                   STUB_CAPTURE=str(self.capture), PUSH="0", FLEET_MSGS_MANIFEST=str(self.manifest),
                   RIG_BASE_IMAGE="devbox:5000/fleet-ros:v1")
        env.update(extra)
        return subprocess.run([str(SCRIPT), "devbox:5000", "t1"], env=env, capture_output=True,
                              text=True)

    def ok(self, **extra):
        proc = self.run_script(**extra)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] \
            if self.log.exists() else []

    def snapshot(self):
        root = self.capture / "0"
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())

    def sources(self):
        return [line.split("\t") for line in
                (self.capture / "sources.tsv").read_text().splitlines()]

    def test_default_argv_carries_no_named_context(self):
        self.ok()
        (call,) = self.calls()
        self.assertEqual(call[:3], ["build", "-t", "devbox:5000/fleet-ros-msgs:t1"])
        self.assertEqual(call[3:7], ["--build-arg", "BASE_IMAGE=devbox:5000/fleet-ros:v1",
                                     "--build-arg", "ROS_DISTRO=lyrical"])
        self.assertEqual(len(call), 8)  # + the staged context, nothing else
        self.assertFalse(any("build-context" in a or "local-sources" in a for a in call))

    def test_dirty_checkout_is_snapshotted_as_git_sees_it(self):
        self.write("src/a_msgs/CMakeLists.txt", "msg/A.msg\nmsg/B.msg\n")  # tracked edit
        self.git("add", "src/a_msgs/CMakeLists.txt")                      # ... staged, even
        self.write("src/a_msgs/msg/B.msg", "uint8 b\n")                    # untracked, new
        self.write("build/a_msgs/junk.o", "ignored build tree\n")          # ignored
        (self.repo / "gone.txt").unlink()                                   # deleted, unstaged
        status = self.git("status", "--porcelain")
        staged = self.git("diff", "--cached", "--name-only")
        before = self.git_state()
        proc = self.ok(FLEET_MSGS_LOCAL=f"a_msgs={self.repo}")
        # nothing written into the checkout's .git: same index bytes, no new objects
        self.assertEqual(self.git_state(), before)
        self.assertEqual(self.snapshot(), [".gitignore", "src/a_msgs/CMakeLists.txt",
                                           "src/a_msgs/msg/A.msg", "src/a_msgs/msg/B.msg"])
        self.assertEqual((self.capture / "0/src/a_msgs/CMakeLists.txt").read_text(),
                         "msg/A.msg\nmsg/B.msg\n")
        self.assertEqual(self.sources(), [["0", "a_msgs", str(self.repo), "v1.0.0-dirty",
                                           "unknown"]])
        # the checkout's own index and working tree are exactly as the developer left them
        self.assertEqual(self.git("status", "--porcelain"), status)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), staged)
        self.assertIn(f"a_msgs <- LOCAL {self.repo} (v1.0.0-dirty)", proc.stderr)
        (call,) = self.calls()
        context = next(a for a in call if a.startswith("local-sources="))
        self.assertEqual(call[call.index(context) - 1], "--build-context")
        self.assertFalse(pathlib.Path(context.split("=", 1)[1]).exists())  # the trap cleaned up
        self.assertEqual(call[call.index("io.rig.build.local=a_msgs") - 1], "--label")

    def test_read_only_checkout_is_snapshotted(self):
        self.write("src/a_msgs/msg/B.msg", "uint8 b\n")
        paths = [self.repo, *self.repo.rglob("*")]
        modes = {p: p.stat().st_mode for p in paths if not p.is_symlink()}
        try:
            for p in modes:
                p.chmod(modes[p] & ~0o222)
            self.ok(FLEET_MSGS_LOCAL=f"a_msgs={self.repo}")
        finally:
            for p, mode in modes.items():
                p.chmod(mode)
        self.assertIn("src/a_msgs/msg/B.msg", self.snapshot())
        self.assertEqual(self.sources()[0][3:], ["v1.0.0-dirty", "unknown"])

    def test_repo_without_commits_is_snapshotted_not_copied_whole(self):
        fresh = self.tmp / "fresh_msgs"
        fresh.mkdir()
        self.git("init", "-q", cwd=fresh)
        self.write(".gitignore", "build/\n", root=fresh)
        self.write("src/f_msgs/msg/F.msg", "uint8 f\n", root=fresh)
        self.write("build/junk.o", "ignored\n", root=fresh)
        self.ok(FLEET_MSGS_LOCAL=f"f_msgs={fresh}")
        self.assertEqual(self.snapshot(), [".gitignore", "src/f_msgs/msg/F.msg"])
        self.assertEqual(self.sources()[0][3:], ["unknown", "unknown"])

    def test_nested_repo_and_symlink(self):
        nested = self.repo / "vendor" / "lib"
        nested.mkdir(parents=True)
        self.git("init", "-q", cwd=nested)
        self.write("vendor/lib/inner.txt", "a nested repo's contents stay out\n")
        (self.repo / "link.msg").symlink_to("src/a_msgs/msg/A.msg")
        self.ok(FLEET_MSGS_LOCAL=f"a_msgs={self.repo}")
        root = self.capture / "0"
        self.assertTrue((root / "vendor/lib").is_dir())
        self.assertEqual(list((root / "vendor/lib").iterdir()), [])
        self.assertEqual(os.readlink(root / "link.msg"), "src/a_msgs/msg/A.msg")

    def git_state(self):
        git_dir = self.repo / ".git"
        return ((git_dir / "index").read_bytes(),
                sorted(str(p.relative_to(git_dir)) for p in (git_dir / "objects").rglob("*")))

    def test_clean_checkout_records_tag_or_sha_and_head(self):
        self.write("build/junk.o", "ignored output does not make a tree dirty\n")
        self.ok(FLEET_MSGS_LOCAL=f"a_msgs={self.repo}")
        self.assertEqual(self.sources()[0][3:], ["v1.0.0", self.head])
        self.write("src/a_msgs/msg/C.msg", "uint8 c\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "unpushed")
        head = self.git("rev-parse", "HEAD")
        self.capture_reset()
        self.ok(FLEET_MSGS_LOCAL=f"a_msgs={self.repo}")
        self.assertEqual(self.sources()[0][3:], [head, head])  # no tag at HEAD: the SHA

    def test_tilde_and_several_entries(self):
        other = self.tmp / "export"  # no .git of its own: copied whole, identity unknown
        self.write("pkg/package.xml", "<package/>\n", root=other)
        self.ok(FLEET_MSGS_LOCAL=f"  a_msgs=~/fleet_msgs\n  b_msgs={other}  ")
        rows = self.sources()
        self.assertEqual([r[:3] for r in rows], [["0", "a_msgs", str(self.repo)],
                                                 ["1", "b_msgs", str(other)]])
        self.assertEqual(rows[1][3:], ["unknown", "unknown"])
        self.assertTrue((self.capture / "1/pkg/package.xml").is_file())
        (call,) = self.calls()
        self.assertIn("io.rig.build.local=a_msgs,b_msgs", call)

    def test_buildx_paths_carry_the_named_context(self):
        self.ok(FLEET_MSGS_LOCAL=f"a_msgs={self.repo}", PUSH="1",
                RIG_BUILD_PLATFORMS="linux/arm64,linux/amd64")
        (call,) = self.calls()
        self.assertEqual(call[:4], ["buildx", "build", "--platform", "linux/arm64,linux/amd64"])
        self.assertIn("--push", call)
        self.assertIn("--build-context", call)
        self.capture_reset()
        self.ok(FLEET_MSGS_LOCAL=f"a_msgs={self.repo}", RIG_BUILD_PLATFORMS="linux/arm64")
        (call,) = self.calls()
        self.assertIn("--load", call)
        self.assertIn("--build-context", call)

    def test_bad_entries_are_refused_before_docker(self):
        for value, needle in (("a_msgs", "is not KEY=PATH"),
                              (f"={self.repo}", "is not KEY=PATH"),
                              ("a_msgs=fleet_msgs", "PATH must be absolute"),
                              (f"a_msgs={self.tmp}/missing", "is not a directory")):
            with self.subTest(value=value):
                proc = self.run_script(FLEET_MSGS_LOCAL=value)
                self.assertEqual(proc.returncode, 1)
                self.assertIn(needle, proc.stderr)
                self.assertEqual(self.calls(), [])

    def test_ssh_repos_without_an_agent(self):
        self.manifest.write_text(MANIFEST.replace("https://github.com/", "git@github.com:"))
        proc = self.run_script()
        self.assertEqual(proc.returncode, 1)  # unchanged: no agent, nothing local -> refused
        self.assertIn("no ssh-agent", proc.stderr)
        self.assertEqual(self.calls(), [])
        # a local checkout may cover the ssh repo: build without forwarding, build_msgs.py
        # refuses any ssh repo left uncovered
        proc = self.ok(FLEET_MSGS_LOCAL=f"a_msgs={self.repo}")
        self.assertIn("building without agent forwarding", proc.stderr)
        (call,) = self.calls()
        self.assertNotIn("--ssh", call)

    def capture_reset(self):
        import shutil
        shutil.rmtree(self.capture, ignore_errors=True)
        self.log.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
