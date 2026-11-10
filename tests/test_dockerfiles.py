"""Properties of every Dockerfile in this repository that a successful build would not report.
Run: python3 tests/test_dockerfiles.py"""
import os
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent
# What BuildKit accepts as a parser directive (frontend/dockerfile/parser/directives.go).
DIRECTIVE = re.compile(r"#\s*([a-zA-Z][a-zA-Z0-9]*)\s*=\s*(.+?)\s*")
KNOWN_DIRECTIVES = {"syntax", "escape", "check"}


def dockerfiles(root=ROOT):
    """Every Dockerfile under ``root``, outside hidden directories (.git, the rig checkout CI
    places in .rig) and the launchers' ignored var/ output."""
    found = []
    for directory, names, files in os.walk(root):
        names[:] = [name for name in names if not name.startswith(".") and name != "var"]
        found += [Path(directory, name) for name in files
                  if re.fullmatch(r"(.+\.)?dockerfile(\..+)?", name, re.I)]
    return sorted(found)


def parser_directives(text):
    """The parser directives BuildKit reads from a Dockerfile, lower-cased name -> value.

    Directives are the leading lines of the form `# name=value` with a known name, after an
    optional shebang. The first line that is anything else, an ordinary comment included, ends
    them; a `# syntax=` further down is only a comment."""
    lines = text.lstrip("﻿").splitlines()
    if lines and lines[0].startswith("#!"):
        lines = lines[1:]
    directives = {}
    for line in lines:
        match = DIRECTIVE.fullmatch(line)
        if not match or match[1].lower() not in KNOWN_DIRECTIVES:
            break
        directives[match[1].lower()] = match[2]
    return directives


class DockerfileTests(unittest.TestCase):
    def test_the_search_covers_the_images_this_repository_builds(self):
        found = {str(path.relative_to(ROOT)) for path in dockerfiles()}
        self.assertLessEqual({"base/Dockerfile", "msgs/Dockerfile"}, found)

    def test_directives_are_read_the_way_the_builder_reads_them(self):
        self.assertEqual(parser_directives("# syntax=docker/dockerfile:1\nFROM scratch\n"),
                         {"syntax": "docker/dockerfile:1"})
        self.assertEqual(parser_directives("# check=skip=all\n#SYNTAX = example.com/frontend \n"),
                         {"check": "skip=all", "syntax": "example.com/frontend"})
        self.assertEqual(parser_directives("#!/usr/bin/env docker\n# syntax=a/b\n"), {"syntax": "a/b"})
        # Inert: after an ordinary comment, a blank line or an instruction.
        for inert in ("# About this image.\n# syntax=a/b\n", "\n# syntax=a/b\n",
                      "FROM scratch\n# syntax=a/b\n", "# check=skip=all\n# note\n# syntax=a/b\n"):
            self.assertNotIn("syntax", parser_directives(inert), inert)

    def test_frontend_is_the_builders_own(self):
        # A `syntax` directive makes every build, cached or not, resolve the named frontend image
        # (docker.io/docker/dockerfile) before its first step: a Docker Hub rate limit or a
        # disconnected builder (`rig build --offline`) then fails the build before anything runs.
        for path in dockerfiles():
            with self.subTest(dockerfile=str(path.relative_to(ROOT))):
                self.assertNotIn("syntax", parser_directives(path.read_text()))


if __name__ == "__main__":
    unittest.main()
