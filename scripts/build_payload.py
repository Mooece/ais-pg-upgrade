"""Build a patch from committed Git trees; never copy a complete installation."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath

UPSTREAM_URL = "https://github.com/DeconstructedCube/aistudio-api"
RELEASE_ID = "20261122-patch-only-443fa3f9"


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args])


def digest(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if (
        not relative
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in relative
        or ":" in relative
    ):
        raise ValueError(f"Unsafe relative path: {relative!r}")
    resolved_root = root.resolve()
    result = root.joinpath(*path.parts)
    if not result.resolve().is_relative_to(resolved_root) or result.resolve() == resolved_root:
        raise ValueError(f"Path escapes output root: {relative!r}")
    return result


def selected(path: str) -> bool:
    return (
        path == "scripts/test.ps1" or path.startswith(("src/aistudio_api/", "web/", "tests/"))
    ) and not path.endswith(".tsbuildinfo")


def build(args: argparse.Namespace) -> dict:
    upstream = Path(args.upstream_repo).resolve()
    custom = Path(args.custom_repo).resolve()
    output = Path(args.output_root).resolve()
    base = git(upstream, "rev-parse", f"{args.base}^{{commit}}").decode().strip()
    custom_commit = git(custom, "rev-parse", f"{args.custom_ref}^{{commit}}").decode().strip()
    payload_root = safe_path(output, "payload")
    if payload_root.is_symlink():
        raise ValueError("Payload root must not be a symbolic link")
    report_path = Path(args.report).resolve() if args.report else None
    if report_path and report_path.is_relative_to(output):
        raise ValueError("Write verification reports outside the source/output root")
    manifest_path = output / "patch-manifest.json"
    safe_path(output, "patch-manifest.json")
    previous_path = (
        Path(args.previous_manifest).resolve() if args.previous_manifest else manifest_path
    )
    previous_entries = {}
    if previous_path.exists():
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        if (
            previous.get("schema_version") != 1
            or previous.get("upstream", {}).get("commit") != base
        ):
            raise ValueError("Previous manifest has an incompatible schema or upstream baseline")
        previous_entries = {entry["path"]: entry for entry in previous["entries"]}
    if git(upstream, "rev-parse", f"{base}^{{tree}}") != git(
        custom, "rev-parse", f"{base}^{{tree}}"
    ):
        raise ValueError("Upstream and custom repositories disagree on the base tree")
    changes = git(custom, "diff", "--no-renames", "--name-status", "-z", base, custom_commit).split(
        b"\0"
    )
    entries = []
    writes = {}
    excluded = []
    for index in range(0, len(changes) - 1, 2):
        status = changes[index].decode("ascii")
        path = changes[index + 1].decode("utf-8")
        if status not in {"A", "M", "D"}:
            raise ValueError(f"Unsupported Git change {status}: {path}")
        old = None if status == "A" else git(upstream, "show", f"{base}:{path}")
        new = None if status == "D" else git(custom, "show", f"{custom_commit}:{path}")
        if not selected(path):
            excluded.append(
                {
                    "path": path,
                    "status": status,
                    "base_bytes": len(old or b""),
                    "custom_bytes": len(new or b""),
                }
            )
            continue
        safe_path(output / "payload", path)
        if old is not None and new is not None and digest(old) == digest(new):
            excluded.append(
                {
                    "path": path,
                    "status": status,
                    "reason": "line-endings-only",
                    "base_bytes": len(old),
                    "custom_bytes": len(new),
                }
            )
            continue
        entry = {
            "path": path,
            "action": "delete" if new is None else "write",
            "base_sha256": None if old is None else digest(old),
            "payload_sha256": None if new is None else digest(new),
        }
        previous_entry = previous_entries.get(path, {})
        accepted = set(previous_entry.get("accepted_sha256", []))
        accepted.update(
            value
            for value in (previous_entry.get("base_sha256"), previous_entry.get("payload_sha256"))
            if value
        )
        accepted.difference_update(
            value for value in (entry["base_sha256"], entry["payload_sha256"]) if value
        )
        if accepted:
            if any(
                len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
                for value in accepted
            ):
                raise ValueError(f"Invalid historical hash for {path}")
            entry["accepted_sha256"] = sorted(accepted)
        entries.append(entry)
        if new is not None:
            writes[path] = new
    old_managed = set()
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous.get("schema_version") != 1:
            raise ValueError("Cannot clean an unknown manifest schema")
        for entry in previous["entries"]:
            if entry["action"] == "write":
                safe_path(payload_root, entry["path"])
                old_managed.add(entry["path"])
    if payload_root.exists():
        existing = {
            path.relative_to(payload_root).as_posix()
            for path in payload_root.rglob("*")
            if path.is_file()
        }
        unknown = existing - old_managed
        if unknown:
            raise ValueError(f"Refusing to replace unmanaged payload files: {sorted(unknown)}")
        # Check all paths before any removal, and never recursively remove directories.
        for path in old_managed:
            target = safe_path(payload_root, path)
            if target.exists() and not target.is_file():
                raise ValueError(f"Managed path is not a file: {target}")
        for path in old_managed:
            target = safe_path(payload_root, path)
            if target.exists():
                target.unlink()
    payload_root.mkdir(parents=True, exist_ok=True)
    for path, data in writes.items():
        target = safe_path(payload_root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    manifest = {
        "schema_version": 1,
        "release_id": RELEASE_ID,
        "upstream": {"url": UPSTREAM_URL, "commit": base},
        "entries": sorted(entries, key=lambda entry: entry["path"]),
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    counts = {}
    for entry in entries:
        group = "runtime" if entry["path"].startswith("src/") else entry["path"].split("/", 1)[0]
        counts.setdefault(group, {"write": 0, "delete": 0})[entry["action"]] += 1
    report = {
        "upstream_commit": base,
        "custom_commit": custom_commit,
        "payload_files": len(writes),
        "payload_bytes": sum(map(len, writes.values())),
        "delete_entries": sum(entry["action"] == "delete" for entry in entries),
        "groups": counts,
        "excluded_paths": excluded,
        "excluded_count": len(excluded),
        "excluded_custom_bytes": sum(entry["custom_bytes"] for entry in excluded),
        "installation": "Requires the separately installed original project; this payload cannot install it.",
        "compatibility": "Exact upstream baseline or already applied identical payload; unknown target changes require installer rejection.",
    }
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-repo", required=True)
    parser.add_argument("--custom-repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--custom-ref", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--report", help="External verification report path")
    parser.add_argument(
        "--previous-manifest",
        help="Previously published manifest for accepted upgrade hashes (defaults to existing output manifest)",
    )
    args = parser.parse_args()
    report = build(args)
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "excluded_paths"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
