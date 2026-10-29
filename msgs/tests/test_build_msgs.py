"""build_msgs.py — the builder-stage half of the overlay — with `git` and `colcon` stubbed on PATH:
the default (every `source:` repo cloned at its pin, an empty local-sources mount changing
nothing), and the FLEET_MSGS_LOCAL path: a local snapshot replaces exactly the clone its key
names, provenance records the host's identity for it (never the declared pin), and keys that
match nothing / several repos / an apt package / one repo twice are refused before any clone.
No daemon, no ROS. Run: python3 msgs/tests/test_build_msgs.py"""
import json
import os
import pathlib
import sys
import tempfile
import unittest

import yaml

MSGS = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MSGS))

import build_msgs  # noqa: E402

SHA = "c0ffee" * 6 + "abcd"

GIT_STUB = f"""#!{sys.executable}
import json, os, sys
a = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps(["git", *a]) + "\\n")
if a[0] == "clone":
    os.makedirs(a[2])
    open(os.path.join(a[2], "CLONED_FROM"), "w").write(a[1])
elif a[2:4] == ["rev-parse", "--verify"] or a[2:4] == ["rev-parse", "HEAD"]:
    print("{SHA}")
"""

COLCON_STUB = f"""#!{sys.executable}
import json, os, sys
a = sys.argv[1:]
prefix = a[a.index("--install-base") + 1]
pkgs = a[a.index("--packages-up-to") + 1:a.index("--cmake-args")]
for p in pkgs:
    os.makedirs(os.path.join(prefix, "share", p), exist_ok=True)
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps(["colcon", *a]) + "\\n")
"""

PX4 = "https://github.com/PX4/px4_msgs.git"
SBG = "https://github.com/christomaszewski/sbg_driver.git"
MANIFEST = {"apt": ["nmea_msgs"],
            "source": [{"repo": PX4, "ref": "v1.16.0", "packages": ["px4_msgs"]},
                       {"repo": SBG, "ref": "v0.11.0", "packages": ["sbg_msgs"]}]}


class BuildMsgs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        for name, body in (("git", GIT_STUB), ("colcon", COLCON_STUB)):
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        self.log = self.tmp / "calls.jsonl"
        self._env = dict(os.environ)
        os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"
        os.environ["STUB_LOG"] = str(self.log)
        os.environ.pop("SSH_AUTH_SOCK", None)
        self.prefix = self.tmp / "prefix"
        self.ws = self.tmp / "ws"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def local_root(self, *rows):
        """A local-sources mount the way build-msgs.sh lays it out: <n>/ snapshots + sources.tsv.
        rows: (key, host path, ref, rev[, kind]); each snapshot holds one marker file."""
        root = self.tmp / "local"
        root.mkdir()
        lines = []
        for n, (key, path, ref, rev, *kind) in enumerate(rows):
            (root / str(n)).mkdir()
            (root / str(n) / "LOCAL_MARKER").write_text(f"{path} {key}")
            lines.append("\t".join([str(n), key, path, ref, rev, *kind]) + "\n")
        if rows:
            (root / "sources.tsv").write_text("".join(lines))
        return str(root)

    def run_build(self, manifest=None, local=None):
        path = self.tmp / "msgs-manifest.yaml"
        path.write_text(yaml.safe_dump(MANIFEST if manifest is None else manifest))
        build_msgs.build(str(path), str(self.prefix), local, str(self.ws))
        return yaml.safe_load((self.prefix / "provenance.yaml").read_text())

    def refused(self, manifest=None, local=None):
        with self.assertRaises(SystemExit) as caught:
            self.run_build(manifest, local)
        self.assertEqual(caught.exception.code, 1)
        return caught.exception

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def clones(self):
        return [c[2] for c in self.calls() if c[:2] == ["git", "clone"]]

    def test_default_clones_every_pin(self):
        prov = self.run_build()
        self.assertEqual(self.clones(), [PX4, SBG])
        self.assertEqual(prov, {"version": 1, "source": [
            {"repo": PX4, "ref": "v1.16.0", "rev": SHA, "packages": ["px4_msgs"]},
            {"repo": SBG, "ref": "v0.11.0", "rev": SHA, "packages": ["sbg_msgs"]}]})

    def test_empty_local_mount_is_the_default(self):
        # msgs/Dockerfile's `local-sources` stage is an empty scratch stage unless build-msgs.sh
        # passes the named context: the mount exists, with no sources.tsv in it.
        empty = self.tmp / "empty"
        empty.mkdir()
        prov = self.run_build(local=str(empty))
        self.assertEqual(self.clones(), [PX4, SBG])
        self.assertNotIn("local", prov["source"][0])

    def test_local_checkout_replaces_exactly_its_clone(self):
        host = "/Users/dev/ws/sbg_driver"
        prov = self.run_build(local=self.local_root(("sbg_msgs", host, "v0.11.0-dirty", "unknown")))
        self.assertEqual(self.clones(), [PX4])  # px4_msgs still cloned at its pin
        built = self.ws / "src" / "repo1"
        self.assertEqual((built / "LOCAL_MARKER").read_text(), f"{host} sbg_msgs")
        self.assertFalse((built / "CLONED_FROM").exists())
        self.assertEqual(prov["source"][1], {"repo": SBG, "ref": "v0.11.0-dirty", "rev": "unknown",
                                             "packages": ["sbg_msgs"], "local": host})
        self.assertEqual(prov["source"][0]["ref"], "v1.16.0")
        colcon = [c for c in self.calls() if c[0] == "colcon"]
        self.assertEqual(len(colcon), 1)
        self.assertIn("sbg_msgs", colcon[0])

    def test_clean_local_checkout_records_its_head(self):
        prov = self.run_build(local=self.local_root(("sbg_msgs", "/w/sbg", "v0.11.0", SHA)))
        self.assertEqual(prov["source"][1]["rev"], SHA)
        self.assertEqual(prov["source"][1]["ref"], "v0.11.0")

    def test_key_forms_name_the_same_repo(self):
        for key in ("sbg_msgs", "sbg_driver", SBG, "https://github.com/christomaszewski/sbg_driver",
                    "git@github.com:christomaszewski/sbg_driver.git",
                    "ssh://git@GitHub.com/christomaszewski/sbg_driver.git"):
            with self.subTest(key=key):
                self.tearDown()
                self.setUp()
                prov = self.run_build(local=self.local_root((key, "/w/sbg", "abc", "unknown")))
                self.assertEqual(self.clones(), [PX4])
                self.assertEqual(prov["source"][1]["local"], "/w/sbg")

    def test_unknown_key_is_refused_before_anything_is_built(self):
        err = self.capture_refusal(local=self.local_root(("sgb_msgs", "/w/sbg", "abc", "unknown")))
        self.assertIn("px4_msgs", err)  # the valid keys are listed
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.prefix.exists())

    def test_refusals_name_the_problem(self):
        cases = {
            "matches no `source:` entry": [("sgb_msgs", "/w", "a", "unknown")],
            "is an `apt:` interface package": [("nmea_msgs", "/w", "a", "unknown")],
            "one checkout per repo": [("sbg_msgs", "/w/a", "a", "unknown"),
                                      ("sbg_driver", "/w/b", "b", "unknown")],
        }
        for needle, rows in cases.items():
            with self.subTest(needle=needle):
                self.tearDown()
                self.setUp()
                err = self.capture_refusal(local=self.local_root(*rows))
                self.assertIn(needle, err)
                self.assertEqual(self.clones(), [])

    def test_ambiguous_repo_name_is_refused_but_the_url_resolves(self):
        twins = {"source": [{"repo": "https://github.com/a/fleet_msgs.git", "ref": "v1",
                             "packages": ["a_msgs"]},
                            {"repo": "https://gitlab.com/b/fleet_msgs.git", "ref": "v2",
                             "packages": ["b_msgs"]}]}
        err = self.capture_refusal(twins, self.local_root(("fleet_msgs", "/w", "a", "unknown")))
        self.assertIn("ambiguous", err)
        self.tearDown()
        self.setUp()
        prov = self.run_build(twins, self.local_root(("gitlab.com/b/fleet_msgs", "/w", "a", "u")))
        self.assertEqual(self.clones(), ["https://github.com/a/fleet_msgs.git"])
        self.assertEqual(prov["source"][1]["local"], "/w")

    def test_apt_only_manifest_refuses_a_local_key(self):
        err = self.capture_refusal({"apt": ["nmea_msgs"]},
                                   self.local_root(("sbg_msgs", "/w", "a", "unknown")))
        self.assertIn("matches no `source:` entry", err)

    def test_uncovered_ssh_repo_without_agent_is_refused_by_name(self):
        ssh = {"source": [{"repo": "git@github.com:acme/private_msgs.git", "ref": "v1",
                           "packages": ["private_msgs"]},
                          {"repo": SBG, "ref": "v0.11.0", "packages": ["sbg_msgs"]}]}
        err = self.capture_refusal(ssh, self.local_root(("sbg_msgs", "/w", "a", "unknown")))
        self.assertIn("git@github.com:acme/private_msgs.git", err)
        self.assertEqual(self.clones(), [])
        # covered by the checkout: no agent needed at all
        self.tearDown()
        self.setUp()
        prov = self.run_build(ssh, self.local_root(("private_msgs", "/w/p", "a", "unknown")))
        self.assertEqual(self.clones(), [SBG])
        self.assertEqual(prov["source"][0]["local"], "/w/p")

    def test_rig_exports_replace_the_clones_with_clone_provenance(self):
        other = "b" * 40
        prov = self.run_build(local=self.local_root((PX4, "-", "v1.16.0", other, "pinned"),
                                                    (SBG, "-", "v0.11.0", SHA, "pinned")))
        self.assertEqual(self.clones(), [])  # nothing fetched in the builder
        self.assertEqual((self.ws / "src" / "repo0" / "LOCAL_MARKER").read_text(), f"- {PX4}")
        self.assertEqual(prov, {"version": 1, "source": [
            {"repo": PX4, "ref": "v1.16.0", "rev": other, "packages": ["px4_msgs"]},
            {"repo": SBG, "ref": "v0.11.0", "rev": SHA, "packages": ["sbg_msgs"]}]})

    def test_a_local_checkout_wins_over_rigs_export(self):
        prov = self.run_build(local=self.local_root((SBG, "-", "v0.11.0", SHA, "pinned"),
                                                    ("sbg_msgs", "/w/sbg", "x-dirty", "unknown")))
        self.assertEqual(self.clones(), [PX4])
        self.assertEqual(prov["source"][1]["local"], "/w/sbg")
        self.assertEqual((self.ws / "src" / "repo1" / "LOCAL_MARKER").read_text(),
                         "/w/sbg sbg_msgs")

    def test_rig_export_mismatches_are_refused(self):
        cases = {"does not declare under `source:`":
                 [("https://example.invalid/x.git", "-", "v1", SHA, "pinned")],
                 "but the manifest pins 'v0.11.0'": [(SBG, "-", "v0.12.0", SHA, "pinned")],
                 "malformed local-sources row": [(SBG, "-", "v0.11.0", SHA, "copied")]}
        for needle, rows in cases.items():
            with self.subTest(needle=needle):
                self.tearDown()
                self.setUp()
                self.assertIn(needle, self.capture_refusal(local=self.local_root(*rows)))
                self.assertEqual(self.calls(), [])

    def test_ssh_repo_covered_by_rigs_export_needs_no_agent(self):
        ssh = {"source": [{"repo": "git@github.com:acme/private_msgs.git", "ref": "v1",
                           "packages": ["private_msgs"]},
                          {"repo": SBG, "ref": "v0.11.0", "packages": ["sbg_msgs"]}]}
        prov = self.run_build(ssh, self.local_root(
            ("git@github.com:acme/private_msgs.git", "-", "v1", SHA, "pinned")))
        self.assertEqual(self.clones(), [SBG])
        self.assertEqual(prov["source"][0], {"repo": "git@github.com:acme/private_msgs.git",
                                             "ref": "v1", "rev": SHA, "packages": ["private_msgs"]})
        self.tearDown()
        self.setUp()
        err = self.capture_refusal(ssh, self.local_root((SBG, "-", "v0.11.0", SHA, "pinned")))
        self.assertIn("neither FLEET_MSGS_LOCAL nor rig's exports cover", err)

    def test_malformed_sources_row_is_refused(self):
        root = pathlib.Path(self.local_root(("sbg_msgs", "/w", "a", "unknown")))
        (root / "sources.tsv").write_text("0\tsbg_msgs\t/w\n")
        self.assertIn("malformed local-sources row", self.capture_refusal(local=str(root)))

    def capture_refusal(self, manifest=None, local=None):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.refused(manifest, local)
        return err.getvalue()


if __name__ == "__main__":
    unittest.main(verbosity=2)
