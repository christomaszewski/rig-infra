"""Opt-in live immutable-pin acceptance using only an already-built local runtime image.

Set RIG_TEST_RUNTIME_IMAGE to the freshly built image. The test creates only unique local
tags, a tiny imported decoy image and its own no-network Compose project, then removes them.
"""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
import uuid


@unittest.skipUnless(os.environ.get("RIG_TEST_RUNTIME_IMAGE"), "set RIG_TEST_RUNTIME_IMAGE for live Docker test")
class NativeImagePins(unittest.TestCase):
    def docker(self, *args, **kwargs):
        result = subprocess.run(["docker", *args], capture_output=True, timeout=60, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.decode().strip()

    def test_compose_starts_validated_id_after_temporary_tag_moves(self):
        image = os.environ["RIG_TEST_RUNTIME_IMAGE"]
        helper = Path(__file__).resolve().parents[1] / "tools/runtime_images.py"
        host_arch = self.docker("info", "--format", "{{.Architecture}}")
        arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(host_arch, host_arch)
        self.assertIn(arch, ("arm64", "amd64"))
        info = json.loads(self.docker("image", "inspect", image))[0]
        self.assertEqual(info["Architecture"], arch)
        self.assertEqual(info["Config"]["Labels"].get("io.rig.rosbag2.replay"), "1")
        image_id = info["Id"]
        project = "rig-infra-pin-test-" + uuid.uuid4().hex[:12]
        alias, decoy = project + ":fixture", project + ":decoy"
        with tempfile.TemporaryDirectory(prefix="rig-infra-pin-test-") as temporary:
            compose = Path(temporary) / "compose.json"
            compose.write_text(json.dumps({"name": project, "services": {"bag-player": {
                "image": alias, "entrypoint": ["/bin/sh", "-c"], "command": ["exit 0"],
                "network_mode": "none", "read_only": True, "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"]}}}))
            self.docker("tag", image, alias)
            try:
                env = dict(os.environ, COMPOSE_PROJECT_NAME=project, RIG_TARGET_ARCH=arch)
                query = subprocess.run([sys.executable, str(helper), "runtime-images", "-f", str(compose)],
                                       env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(query.returncode, 0, query.stderr)
                self.assertEqual(json.loads(query.stdout)["images"], [alias])
                env["RIG_RUNTIME_IMAGE_PINS"] = json.dumps({"version": 1, "images": {alias: image_id}})
                # No second base image/download is needed: import unique, deliberately non-runnable bytes.
                archive = io.BytesIO()
                with tarfile.open(fileobj=archive, mode="w") as tar:
                    marker = project.encode()
                    entry = tarfile.TarInfo("test-marker")
                    entry.size = len(marker)
                    tar.addfile(entry, io.BytesIO(marker))
                self.docker("import", "--platform", "linux/" + arch, "-", decoy, input=archive.getvalue())
                self.docker("tag", decoy, alias)
                self.assertNotEqual(json.loads(self.docker("image", "inspect", alias))[0]["Id"], image_id)
                launch = subprocess.run([sys.executable, str(helper), "pinned-up", "-f", str(compose),
                                         "--", "--detach"], env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(launch.returncode, 0, launch.stderr)
                container = self.docker("ps", "-aq", "--filter", "label=com.docker.compose.project=" + project)
                self.assertTrue(container)
                actual = json.loads(self.docker("inspect", container))[0]
                self.assertEqual(actual["Image"], image_id)
                self.assertEqual(self.docker("wait", container), "0")
            finally:
                subprocess.run(["docker", "compose", "-p", project, "-f", str(compose),
                                "down", "--remove-orphans", "--timeout", "1"], capture_output=True, timeout=30)
                for ref in (alias, decoy):
                    subprocess.run(["docker", "image", "rm", ref], capture_output=True, timeout=30)


if __name__ == "__main__":
    unittest.main()
