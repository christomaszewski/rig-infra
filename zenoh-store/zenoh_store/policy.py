"""Fetch rules and encodings: what a file is, and whether it crosses the link on its own.

Rules come from the store's config (config.py) and are matched first-to-last against the
store-relative path. The first match decides `fetch` (auto | latest | on_request) and `priority`;
a file no rule matches is `on_request` at `default_priority`. A rule's `max_mb` demotes bigger files
to `on_request` (they are still listed and fetchable on demand).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Extension -> encoding. Only picks the reply's Zenoh encoding: every file is served as bytes.
DEFAULT_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "tif": "image/tiff",
    "tiff": "image/tiff", "webp": "image/webp",
    "json": "application/json", "geojson": "application/geo+json", "yaml": "application/yaml",
    "yml": "application/yaml", "xml": "application/xml", "csv": "text/csv", "txt": "text/plain",
    "log": "text/plain", "md": "text/markdown",
    "las": "application/vnd.las", "laz": "application/vnd.laszip", "pcd": "application/x-pcd",
    "ply": "application/x-ply", "e57": "application/x-e57",
    "mcap": "application/x-mcap", "db3": "application/vnd.sqlite3", "bag": "application/x-rosbag",
    "kml": "application/vnd.google-earth.kml+xml", "kmz": "application/vnd.google-earth.kmz",
    "mp4": "video/mp4", "mkv": "video/x-matroska",
    "gz": "application/gzip", "zst": "application/zstd", "zip": "application/zip",
    "tar": "application/x-tar", "bin": "application/octet-stream",
}
OCTET = "application/octet-stream"


def glob_regex(pattern: str) -> "re.Pattern":
    """A path glob as a regex: `*` and `?` stay within one directory level, `**` spans levels
    (`a/**/b` also matches `a/b`), `{x,y}` is an alternation, `[...]` a character class."""
    out, i, n = [], 0, len(pattern)
    while i < n:
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "{":
            end = pattern.find("}", i)
            if end < 0:
                raise ValueError(f"glob {pattern!r}: unclosed '{{'")
            out.append("(?:" + "|".join(re.escape(alt) for alt in pattern[i + 1:end].split(",")) + ")")
            i = end + 1
        elif c == "[":
            end = pattern.find("]", i + 2)
            if end < 0:
                raise ValueError(f"glob {pattern!r}: unclosed '['")
            body = pattern[i + 1:end]
            out.append("[" + ("^" + body[1:] if body.startswith("!") else body) + "]")
            i = end + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


@dataclass(frozen=True)
class Rule:
    match: str
    fetch: str
    priority: Optional[int]
    max_bytes: Optional[int]
    regex: "re.Pattern"

    def describe(self) -> dict:
        d = {"match": self.match, "fetch": self.fetch}
        if self.priority is not None:
            d["priority"] = self.priority
        if self.max_bytes is not None:
            d["max_mb"] = round(self.max_bytes / 1048576, 3)
        return d


class Policy:
    def __init__(self, rules=(), mime=None, default_priority: int = 100):
        self.default_priority = default_priority
        self.rules = [Rule(r["match"], r["fetch"], r.get("priority"),
                           int(r["max_mb"] * 1048576) if r.get("max_mb") else None, glob_regex(r["match"]))
                      for r in rules]
        self.mime_table = dict(DEFAULT_MIME)
        self.mime_table.update({k.lower(): v for k, v in (mime or {}).items()})

    @classmethod
    def from_settings(cls, settings: dict) -> "Policy":
        return cls(settings.get("fetch", ()), settings.get("mime"), settings.get("default_priority", 100))

    def classify(self, relpath: str, size: int) -> tuple:
        """(fetch, priority) for a file."""
        for rule in self.rules:
            if rule.regex.match(relpath):
                priority = self.default_priority if rule.priority is None else rule.priority
                if rule.fetch != "on_request" and rule.max_bytes is not None and size > rule.max_bytes:
                    return "on_request", priority
                return rule.fetch, priority
        return "on_request", self.default_priority

    def mime(self, relpath: str) -> str:
        name = relpath.rsplit("/", 1)[-1]
        if "." not in name:
            return OCTET
        return self.mime_table.get(name.rsplit(".", 1)[-1].lower(), OCTET)

    def describe(self) -> dict:
        return {"rules": [r.describe() for r in self.rules], "default": "on_request",
                "default_priority": self.default_priority}
