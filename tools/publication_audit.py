#!/usr/bin/env python3
"""Read-only, value-free audit of an explicitly selected immutable Git snapshot.

This heuristic audit never authorizes publication or export of existing history.
It does not execute selected code or intentionally request account credentials.
Repository-local Git configuration/storage must be trusted; this is not a sandbox.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit

MAX_BLOB = 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
MAX_OBJECTS = 20000
MAX_COMMITS = 5000
RULES = {
    "credential-token": re.compile(r"\b(?:sk-(?:proj-|ant-[A-Za-z0-9]+-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})", re.I),
    "private-key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"),
    "bearer-token": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{12,}", re.I),
    "credential-assignment": re.compile(r"""\b(?:access_token|refresh_token|id_token|api_key|password)\b["']?\s*[:=]\s*["']?[A-Za-z0-9._~+/-]{12,}""", re.I),
    "private-receiver-url": re.compile(r"https?://(?:claude\.ai|claude\.com)/(?:code|chat|remote-control)/[^\s\]\"'<>]+", re.I),
    "personal-home-path": re.compile(r"(?:/home/[^/\s\"']+/|/Users/[^/\s\"']+/|[A-Z]:[\\/]+Users[\\/]+[^\\/\s\"']+[\\/]+|\\\\wsl(?:\$|\.localhost)\\)", re.I),
    "email-address": re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
}
REASONS = {"synthetic-test-fixture", "sanitizer-pattern", "public-example"}
PRIVATE_PARTS = {".git", ".audit", "_sessions", ".codex", ".claude", ".relay", ".ascend-relay"}
PRIVATE_NAMES = {"auth.json", "credentials.json", ".credentials.json"}
LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
SAFE_PATH = re.compile(r"[A-Za-z0-9_. /-]+\Z")


class AuditError(Exception):
    """A generic refusal; never include raw input or subprocess diagnostics."""


def sha(body):
    return hashlib.sha256(body).hexdigest()


def strict_json(body):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise AuditError("duplicate JSON key")
            value[key] = item
        return value
    try:
        return json.loads(body.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(AuditError("nonstandard JSON constant")))
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise AuditError("invalid manifest encoding or JSON") from None


def valid_path(path):
    if not isinstance(path, str) or not SAFE_PATH.fullmatch(path):
        return False
    parts = path.split("/")
    return (bool(parts) and all(part not in {"", ".", ".."} and not part.endswith((".", " ")) for part in parts)
            and not path.startswith("/") and str(PurePosixPath(path)) == path
            and not any(part.casefold() in PRIVATE_PARTS for part in parts)
            and not any(part.casefold() in PRIVATE_NAMES or part.casefold().startswith(".env")
                        or re.search(r"\.(?:sqlite3?|db)(?:-wal|-shm)?$", part, re.I)
                        for part in parts))


def scan(body, location):
    try:
        decoded = body.decode("utf-8")
    except UnicodeError:
        raise AuditError("selected content is not strict UTF-8") from None
    if "\x00" in decoded:
        raise AuditError("selected content contains NUL")
    found = []
    for number, line in enumerate(decoded.splitlines(), 1):
        for rule, pattern in RULES.items():
            if pattern.search(line):
                found.append({"path": location, "line": number, "rule": rule,
                              "line_sha256": sha(line.encode("utf-8"))})
    return found


class Objects:
    def __init__(self, repo, profile):
        self.repo = Path(repo).resolve(strict=True)
        self.profile = profile
        self.env = {
            "PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "HOME": str(profile),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }
        self.count = 0
        self.total = 0
        self.cache = {}
        try:
            if not stat.S_ISDIR((self.repo / ".git").lstat().st_mode):
                raise AuditError("publication audit requires an independent Git directory")
            for relative in ("shallow", "info/grafts", "objects/info/alternates"):
                if os.path.lexists(self.repo / ".git" / relative):
                    raise AuditError("shallow, grafted or alternate Git storage is unsupported")
        except OSError:
            raise AuditError("independent Git directory unavailable") from None
        if self.git("rev-parse", "--show-toplevel").decode().strip() != str(self.repo):
            raise AuditError("unexpected Git root")
        common = self.git("rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()
        if common != str(self.repo / ".git"):
            raise AuditError("unexpected Git common directory")
        algorithm = self.git("rev-parse", "--show-object-format").decode().strip()
        if algorithm not in {"sha1", "sha256"}:
            raise AuditError("unsupported Git object format")
        self.algorithm = algorithm
        self.oid_bytes = 20 if algorithm == "sha1" else 32

    def git(self, *args, bound=MAX_BLOB):
        with tempfile.TemporaryFile(dir=self.profile) as output, tempfile.TemporaryFile(dir=self.profile) as errors:
            try:
                result = subprocess.run(
                    ["/usr/bin/git", "--no-replace-objects", "-c", "protocol.allow=never",
                     "-c", "core.hooksPath=/dev/null", *args], cwd=self.repo,
                    env=self.env, stdin=subprocess.DEVNULL, stdout=output, stderr=errors, timeout=20)
            except (OSError, subprocess.TimeoutExpired):
                raise AuditError("bounded Git read failed") from None
            if result.returncode != 0 or output.tell() > bound:
                raise AuditError("Git read failed or exceeded bound")
            output.seek(0)
            return output.read()

    def object(self, oid, kind):
        if not re.fullmatch(r"[0-9a-f]{" + str(self.oid_bytes * 2) + r"}", oid):
            raise AuditError("full lowercase object identity required")
        if (oid, kind) in self.cache:
            return self.cache[oid, kind]
        self.count += 1
        if self.count > MAX_OBJECTS:
            raise AuditError("object count exceeds audit bound")
        size = self.git("cat-file", "-s", oid, bound=64).strip()
        if not size.isdigit() or int(size) > MAX_BLOB:
            raise AuditError("object exceeds audit bound")
        body = self.git("cat-file", kind, oid)
        header = kind.encode() + b" " + str(len(body)).encode() + b"\x00"
        if len(body) != int(size) or hashlib.new(self.algorithm, header + body).hexdigest() != oid:
            raise AuditError("Git object identity mismatch")
        self.total += len(body)
        if self.total > MAX_TOTAL:
            raise AuditError("aggregate audit bytes exceed bound")
        self.cache[oid, kind] = body
        return body

    def tree(self, oid):
        leaves = {}
        def visit(tree_oid, prefix, depth):
            if depth > 64:
                raise AuditError("tree depth exceeds bound")
            data = self.object(tree_oid, "tree")
            position = 0
            names = set()
            while position < len(data):
                zero = data.find(b"\x00", position)
                if zero < 0 or zero + 1 + self.oid_bytes > len(data):
                    raise AuditError("invalid tree encoding")
                try:
                    mode, name_bytes = data[position:zero].split(b" ", 1)
                    name = name_bytes.decode("utf-8")
                except (ValueError, UnicodeError):
                    raise AuditError("unsupported tree entry") from None
                if not name or name in {".", ".."} or "/" in name or name in names:
                    raise AuditError("ambiguous tree entry")
                names.add(name)
                child = data[zero + 1:zero + 1 + self.oid_bytes].hex()
                position = zero + 1 + self.oid_bytes
                path = prefix + name
                if mode == b"40000":
                    visit(child, path + "/", depth + 1)
                else:
                    leaves[path] = (mode.decode("ascii"), child)
                    if len(leaves) > MAX_OBJECTS:
                        raise AuditError("tree entry count exceeds bound")
        visit(oid, "", 0)
        return leaves


def commit_parts(objects, oid):
    body = objects.object(oid, "commit")
    headers, separator, message = body.partition(b"\n\n")
    if not separator:
        raise AuditError("invalid commit encoding")
    trees = [line[5:].decode("ascii") for line in headers.splitlines() if line.startswith(b"tree ")]
    parents = [line[7:].decode("ascii") for line in headers.splitlines() if line.startswith(b"parent ")]
    if len(trees) != 1:
        raise AuditError("invalid commit tree")
    return trees[0], parents, body


def policy(body):
    value = strict_json(body)
    if (not isinstance(value, dict) or set(value) != {"schema", "files", "exceptions"}
            or type(value["schema"]) is not int or value["schema"] != 1
            or not isinstance(value["files"], list) or not value["files"]
            or not all(valid_path(path) for path in value["files"])
            or len(value["files"]) != len({path.casefold() for path in value["files"]})
            or not isinstance(value["exceptions"], list)):
        raise AuditError("invalid closed publication manifest")
    exceptions = {}
    for item in value["exceptions"]:
        if (not isinstance(item, dict) or set(item) != {"path", "rule", "line_sha256", "reason"}
                or not isinstance(item["path"], str) or item["path"] not in value["files"]
                or not isinstance(item["rule"], str) or item["rule"] not in RULES
                or not isinstance(item["line_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["line_sha256"])
                or not isinstance(item["reason"], str) or item["reason"] not in REASONS):
            raise AuditError("invalid exact publication exception")
        key = (item["path"], item["rule"], item["line_sha256"])
        if key in exceptions:
            raise AuditError("duplicate publication exception")
        exceptions[key] = item["reason"]
    return value["files"], exceptions


def audit(repo: Path, revision: str, manifest_path="tools/publication-manifest.json"):
    if not valid_path(manifest_path):
        raise AuditError("invalid manifest path")
    auditor_file = Path(__file__).read_bytes()
    with tempfile.TemporaryDirectory(prefix="relay-publication-read-") as temporary:
        objects = Objects(repo, Path(temporary))
        tree_oid, _, _ = commit_parts(objects, revision)
        tree = objects.tree(tree_oid)
        def selected(path):
            if path not in tree or tree[path][0] not in {"100644", "100755"}:
                raise AuditError("selected path missing or not a regular Git blob")
            return objects.object(tree[path][1], "blob")
        manifest = selected(manifest_path)
        files, exceptions = policy(manifest)
        findings, details, used = [], {}, set()
        for path in files:
            # A sensitive filename is never printed in a finding or receipt.
            if scan(path.encode(), "selected-path"):
                raise AuditError("selected pathname requires private review")
            body = selected(path)
            details[path] = {"oid": tree[path][1], "sha256": sha(body),
                             "mode": tree[path][0], "size": len(body)}
            matches = scan(body, path)
            for finding in matches:
                key = (path, finding["rule"], finding["line_sha256"])
                if key in exceptions:
                    if key in used:
                        raise AuditError("exception matches more than one occurrence")
                    used.add(key)
                else:
                    findings.append(finding)
            if path.endswith(".md"):
                for number, line in enumerate(body.decode("utf-8").splitlines(), 1):
                    for link in LINK.findall(line):
                        link = link.strip("<>").split('"', 1)[0].strip()
                        try:
                            target = urlsplit(link)
                        except ValueError:
                            raise AuditError("invalid documentation link") from None
                        if target.scheme or target.netloc or not target.path:
                            continue
                        parts = list(PurePosixPath(path).parent.parts)
                        for part in unquote(target.path).split("/"):
                            if part == "..":
                                if parts:
                                    parts.pop()
                                else:
                                    parts = ["<outside>"]
                                    break
                            elif part not in {"", "."}:
                                parts.append(part)
                        if target.path.startswith("/") or "/".join(parts) not in files:
                            findings.append({"path": path, "line": number, "rule": "missing-local-link",
                                             "line_sha256": sha(line.encode("utf-8"))})
        if used != set(exceptions):
            raise AuditError("stale publication exception")
        history_findings, queue, visited = [], [revision], set()
        while queue:
            oid = queue.pop()
            if oid in visited:
                continue
            visited.add(oid)
            if len(visited) > MAX_COMMITS:
                raise AuditError("history count exceeds audit bound")
            _, parents, body = commit_parts(objects, oid)
            history_findings.extend(scan(body, "commit:" + oid))
            queue.extend(parents)
        if Path(__file__).read_bytes() != auditor_file:
            raise AuditError("auditor file changed during observation")
        return {
            "schema": 1, "revision": revision, "tree": tree_oid,
            "manifest_sha256": sha(manifest), "auditor_file_sha256": sha(auditor_file),
            "auditor_loaded_bytes_verified": False,
            "snapshot_checks_passed": not findings, "findings": findings,
            "files": details, "selected_files": len(files), "omitted_tree_entries": len(tree) - len(files),
            "exceptions_used": len(used), "history_commits_checked": len(visited),
            "history_findings": history_findings, "historical_blobs_scanned": False,
            "existing_history_export_allowed": False, "publication_authorized": False,
            "trusted_git_metadata_required": True,
            "scope": "selected immutable UTF-8 blobs and all reachable commit metadata; heuristic categories only",
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--revision", required=True, help="full immutable commit OID; no branch/ref")
    parser.add_argument("--manifest", default="tools/publication-manifest.json",
                        help="exact manifest path in that same commit")
    args = parser.parse_args(argv)
    try:
        report = audit(args.repo, args.revision, args.manifest)
    except (AuditError, OSError, UnicodeError, ValueError, TypeError):
        print(json.dumps({"schema": 1, "snapshot_checks_passed": False,
                          "publication_authorized": False, "error": "audit refused; incomplete or unsupported input"}))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if report["snapshot_checks_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
