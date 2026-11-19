"""Exercise the environment passed by a rendered record.sh to ros2 bag record."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import bag_cmd


class RecordingScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        _, rendered, _ = bag_cmd.render({"name": "logger"}, self.root)
        # A baked deployment ships this same rendered file and can start it after a run rotates.
        self.script = self.root / "portable" / "record.sh"
        self.script.parent.mkdir()
        shutil.copy(rendered, self.script)
        stub = self.root / "ros2"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "print(json.dumps([os.getenv('RIG_PUBLICATION_SCOPE', ''), sys.argv[-1]]))\n"
        )
        stub.chmod(0o755)

    def record(self, root, *, override=""):
        env = {**os.environ, "PATH": f"{self.root}:{os.environ['PATH']}", "ROS_DISTRO": "",
               "RIG_BAG_ROOT": str(root), "RIG_PUBLICATION_SCOPE": override}
        completed = subprocess.run(["bash", self.script], env=env, text=True, capture_output=True, check=True)
        scope, path = json.loads(completed.stdout)
        return scope, Path(path).parent, completed.stderr

    def open_run(self, root, directory, document):
        run = root / "runs" / directory
        run.mkdir(parents=True)
        (run / "manifest.yaml").write_text(yaml.safe_dump(document))
        current = root / "current"
        current.unlink(missing_ok=True)
        current.symlink_to(run.relative_to(root))
        return run

    def test_independent_roots_use_shared_uuid_and_refresh_on_restart(self):
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        for host in ("port", "starboard"):
            root = self.root / host
            for label, value in (("survey", first), ("next", second)):
                run = self.open_run(root, f"{host}-{label}", {"run_uuid": value})
                scope, parent, _ = self.record(root)
                self.assertEqual((scope, parent), (value, run / "bags/logger"))

    def test_override_and_legacy_fallback(self):
        run = self.open_run(self.root, "test", {"run_uuid": str(uuid.uuid4())})
        self.assertEqual(self.record(self.root, override="shared:legacy")[0], "shared:legacy")
        for text in ("run: test\n", "run_uuid: [bad]\n", f"run_uuid: {uuid.UUID(int=0)}\n",
                     "run_uuid: $(echo anything)\n", "--- [bad\n"):
            (run / "manifest.yaml").write_text(text)
            scope, parent, _ = self.record(self.root)
            self.assertEqual((scope, parent), ("", run / "bags/logger"))
        (self.root / "current").unlink()
        self.assertEqual(self.record(self.root)[:2], ("", self.root / "bags/logger"))


if __name__ == "__main__":
    unittest.main()
