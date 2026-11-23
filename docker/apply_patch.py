#!/usr/bin/env python3
"""Validate and patch an installed original Docker runtime during image build."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import upgrade as u

MARKERS = ("main.py", "src/aistudio_api/main.py", "src/aistudio_api/domain/models.py")
RUNTIME_PREFIX = "src/aistudio_api/"


def installed(project: Path) -> None:
    """Probe the image's real global Python and preinstalled native IPC signer."""
    for relative in MARKERS:
        if not u.plain_path(project / relative).is_file():
            raise u.UpgradeError(f"Install the original Docker image first; missing {relative}")
    signer = u.plain_path(project / "waa-signer/bin/waa-signer")
    if not signer.is_file():
        raise u.UpgradeError("Original Docker image requires its built native WAA signer")
    probe = (
        "import sys; assert sys.version_info >= (3,11); "
        "import fastapi,httpx,pydantic,uvicorn,dotenv,yaml,multipart,h2,socksio; "
        "assert int(pydantic.__version__.split('.')[0]) >= 2"
    )
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHON_DOTENV_DISABLED="1")
    try:
        python = subprocess.run(
            [sys.executable, "-I", "-B", "-c", probe],
            cwd=project,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if python.returncode:
            raise u.UpgradeError("Original image fails Python >=3.11 / dependency import check")
        sign = subprocess.run(
            [str(signer), "-check"],
            cwd=project,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        result = json.loads(sign.stdout)
        if (
            sign.returncode
            or not isinstance(result, dict)
            or result.get("ok") is not True
            or result.get("mode") != "ipc"
        ):
            raise u.UpgradeError("Original image signer -check must return ok=true, mode=ipc")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise u.UpgradeError(
            f"Original image installation probe failed: {type(exc).__name__}"
        ) from exc


def _entry(entry: object, seen: set[str]) -> tuple[str, str, str | None, str | None, list[str]]:
    if not isinstance(entry, dict):
        raise u.UpgradeError("Invalid manifest entry")
    relative = u.safe_relative(entry.get("path"))
    if relative.casefold() in seen:
        raise u.UpgradeError(f"Duplicate patch path: {relative}")
    seen.add(relative.casefold())
    action, base, wanted = (entry.get(key) for key in ("action", "base_sha256", "payload_sha256"))
    accepted = entry.get("accepted_sha256", [])
    if (
        action not in ("write", "delete")
        or any(
            value is not None
            and (not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None)
            for value in (base, wanted)
        )
        or (action == "write" and wanted is None)
        or (action == "delete" and wanted is not None)
    ):
        raise u.UpgradeError(f"Invalid action/checksum: {relative}")
    if not isinstance(accepted, list) or any(
        not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
        for value in accepted
    ):
        raise u.UpgradeError(f"Invalid accepted checksums: {relative}")
    return relative, action, base, wanted, accepted


def validate_package(package: Path = ROOT) -> dict:
    """Validate every declared payload and its inventory without a Docker daemon."""
    package = u.plain_path(package)
    manifest = u.read_json(package / "patch-manifest.json")
    if (
        manifest.get("schema_version") != 1
        or not isinstance(manifest.get("release_id"), str)
        or not manifest["release_id"]
        or manifest.get("upstream") != {"url": u.UPSTREAM, "commit": u.BASE}
        or not isinstance(manifest.get("entries"), list)
    ):
        raise u.UpgradeError("Unsupported or invalid patch manifest")
    seen: set[str] = set()
    writes: set[str] = set()
    runtime_entries = 0
    for raw in manifest["entries"]:
        relative, action, _, wanted, _ = _entry(raw, seen)
        if action == "write":
            writes.add(relative)
            payload_path = u.plain_path(package / "payload" / relative)
            if not payload_path.is_file():
                raise u.UpgradeError(f"Missing payload file: {relative}")
            if u.digest(payload_path.read_bytes()) != wanted:
                raise u.UpgradeError(f"Corrupt payload: {relative}")
        runtime_entries += relative.startswith(RUNTIME_PREFIX)
    if not runtime_entries:
        raise u.UpgradeError("Patch manifest contains no runtime files")
    payload_root = u.plain_path(package / "payload")
    for path in payload_root.rglob("*"):
        u.plain_path(path)
        if path.is_file() and path.relative_to(payload_root).as_posix() not in writes:
            raise u.UpgradeError("Payload contains an unmanaged file")
    return manifest


def preflight(project: Path, package: Path = ROOT) -> tuple[dict, list[dict]]:
    """Validate the entire payload; inspect and change only installed runtime files."""
    project, package = u.plain_path(project), u.plain_path(package)
    installed(project)
    manifest = validate_package(package)
    changes: list[dict] = []
    for entry in manifest["entries"]:
        relative = entry["path"]
        if not relative.startswith(RUNTIME_PREFIX):
            continue
        action, base, wanted = (entry[key] for key in ("action", "base_sha256", "payload_sha256"))
        accepted = entry.get("accepted_sha256", [])
        target = u.plain_path(project / relative)
        if target.exists() and not target.is_file():
            raise u.UpgradeError(f"Target is not a regular file: {relative}")
        current = u.digest(target.read_bytes()) if target.is_file() else None
        if (action == "write" and current == wanted) or (action == "delete" and current is None):
            continue
        if current != base and (current is None or current not in accepted):
            raise u.UpgradeError(f"Unknown target changes: {relative}")
        payload = None
        if action == "write":
            payload = u.plain_path(package / "payload" / relative).read_bytes()
            if u.digest(payload) != wanted:
                raise u.UpgradeError(f"Payload changed during preflight: {relative}")
        changes.append(
            {
                "path": relative,
                "action": action,
                "before": current,
                "after": wanted,
                "payload": payload,
            }
        )
    return manifest, changes


def _receipt_path(project: Path, package: Path, receipt: Path) -> Path:
    receipt = u.plain_path(receipt)
    u.backup_root(project, package, str(receipt.parent))
    if receipt.exists() and not receipt.is_file():
        raise u.UpgradeError("Receipt is not a regular file")
    return receipt


def _receipt_bytes(manifest: dict, package: Path) -> bytes:
    entries = [entry for entry in manifest["entries"] if entry["path"].startswith(RUNTIME_PREFIX)]
    receipt = {
        "schema_version": 1,
        "status": "applied",
        "release_id": manifest["release_id"],
        "upstream": manifest["upstream"],
        "manifest_sha256": u.digest((package / "patch-manifest.json").read_bytes(), raw=True),
        "runtime_writes": sum(entry["action"] == "write" for entry in entries),
        "runtime_deletions": sum(entry["action"] == "delete" for entry in entries),
    }
    return (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode()


def apply(
    project: Path, backup_dir: str, package: Path = ROOT, receipt: Path | None = None
) -> Path | None:
    project, package = u.plain_path(project), u.plain_path(package)
    manifest, changes = preflight(project, package)
    root = u.backup_root(project, package, backup_dir)
    receipt = _receipt_path(project, package, receipt) if receipt is not None else None
    receipt_data = _receipt_bytes(manifest, package) if receipt is not None else None
    if not changes:
        if receipt is not None:
            u.atomic_write(receipt, receipt_data)
        return None
    backup = root / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:12])
    backup.mkdir(parents=True)
    record = {
        "schema_version": 1,
        "project": str(project),
        "release_id": manifest["release_id"],
        "status": "prepared",
        "entries": [],
    }
    receipt_before = receipt.read_bytes() if receipt is not None and receipt.is_file() else None
    if receipt is not None:
        record["docker_receipt"] = {
            "path": str(receipt),
            "before_sha256": u.digest(receipt_before, raw=True)
            if receipt_before is not None
            else None,
            "after_sha256": u.digest(receipt_data, raw=True),
            "metadata": u.metadata(receipt) if receipt.is_file() else None,
        }
        if receipt_before is not None:
            u.atomic_write(backup / "receipt-before.json", receipt_before)
    for change in changes:
        target = project / change["path"]
        entry = {key: value for key, value in change.items() if key != "payload"}
        entry["metadata"] = u.metadata(target) if target.exists() else None
        entry["backup_sha256"] = None
        if target.exists():
            data = target.read_bytes()
            entry["backup_sha256"] = u.digest(data, raw=True)
            u.atomic_write(backup / "files" / change["path"], data)
        record["entries"].append(entry)
    u.save_record(backup, record)
    print(f"Docker runtime backup prepared: {backup}", flush=True)
    applied: list[dict] = []
    receipt_written = False
    try:
        for change, entry in zip(changes, record["entries"]):
            target = u.plain_path(project / change["path"])
            current = u.digest(target.read_bytes()) if target.is_file() else None
            if current != entry["before"]:
                raise u.UpgradeError(f"Target changed during apply: {entry['path']}")
            if change["action"] == "write":
                u.atomic_write(target, change["payload"], entry["metadata"])
            else:
                target.unlink()
            applied.append(entry)
        if receipt is not None:
            current_receipt = receipt.read_bytes() if receipt.is_file() else None
            if current_receipt != receipt_before:
                raise u.UpgradeError("Receipt changed during apply")
            u.atomic_write(receipt, receipt_data, record["docker_receipt"]["metadata"])
            receipt_written = True
        record["status"] = "applied"
        u.save_record(backup, record)
    except Exception as exc:
        recovery_errors = []
        for entry in reversed(applied):
            try:
                u.restore_entry(project, backup, entry)
            except OSError:
                recovery_errors.append(entry["path"])
        if receipt_written:
            try:
                if receipt_before is None:
                    receipt.unlink()
                else:
                    u.atomic_write(receipt, receipt_before, record["docker_receipt"]["metadata"])
            except OSError:
                recovery_errors.append("docker receipt")
        record["status"] = "recovery_required" if recovery_errors else "rolled_back"
        u.save_record(backup, record)
        if recovery_errors:
            raise u.UpgradeError(
                f"Docker patch failed and recovery needs retry; backup: {backup}; files: {', '.join(recovery_errors)}"
            ) from exc
        raise
    return backup


def _check_rollback_code(backup: Path, record: dict, project: Path) -> None:
    """Preflight the same code/backup invariants before invalidating a receipt."""
    if record.get("schema_version") != 1 or not isinstance(record.get("entries"), list):
        raise u.UpgradeError("Invalid Docker backup manifest")
    u.backup_root(project, ROOT, str(backup))
    seen: set[str] = set()
    for entry in record["entries"]:
        if (
            not isinstance(entry, dict)
            or not {"path", "action", "before", "after", "metadata", "backup_sha256"}
            <= entry.keys()
        ):
            raise u.UpgradeError("Invalid Docker backup entry")
        relative = u.safe_relative(entry["path"])
        if not relative.startswith(RUNTIME_PREFIX) or relative.casefold() in seen:
            raise u.UpgradeError("Invalid or duplicate Docker runtime backup path")
        seen.add(relative.casefold())
        if any(
            value is not None
            and (not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None)
            for value in (entry["before"], entry["after"], entry["backup_sha256"])
        ):
            raise u.UpgradeError(f"Invalid Docker backup checksum: {relative}")
        if (
            entry["action"] not in ("write", "delete")
            or (entry["action"] == "write") != (entry["after"] is not None)
            or (
                entry["before"] is None
                and (entry["backup_sha256"] is not None or entry["metadata"] is not None)
            )
        ):
            raise u.UpgradeError(f"Inconsistent Docker backup entry: {relative}")
        target = u.plain_path(project / relative)
        current = u.digest(target.read_bytes()) if target.is_file() else None
        allowed = (
            {entry["after"]} if record["status"] == "applied" else {entry["before"], entry["after"]}
        )
        if record["status"] == "rolled_back":
            allowed = {entry["before"]}
        if (target.exists() and not target.is_file()) or current not in allowed:
            raise u.UpgradeError(f"Target changed after upgrade: {relative}")
        if entry["before"] is not None:
            original = u.plain_path(backup / "files" / relative).read_bytes()
            if (
                u.digest(original, raw=True) != entry["backup_sha256"]
                or u.digest(original) != entry["before"]
            ):
                raise u.UpgradeError(f"Backup file checksum mismatch: {relative}")
            meta = entry["metadata"]
            if (
                not isinstance(meta, dict)
                or any(not isinstance(meta.get(key), int) for key in ("mode", "uid", "gid"))
                or not 0 <= meta["mode"] <= 0o7777
                or meta["uid"] < 0
                or meta["gid"] < 0
            ):
                raise u.UpgradeError(f"Invalid Docker backup metadata: {relative}")


def rollback(backup: Path) -> Path:
    backup = u.plain_path(backup)
    raw = u.plain_path(backup / "backup-manifest.json").read_bytes()
    if (
        u.digest(raw, raw=True)
        != u.plain_path(backup / "backup-manifest.sha256").read_text().strip()
    ):
        raise u.UpgradeError("Backup manifest checksum mismatch")
    record = u.read_json(backup / "backup-manifest.json")
    receipt_info = record.get("docker_receipt")
    if receipt_info is None:
        return u.rollback(backup)
    if record.get("status") not in (
        "applied",
        "prepared",
        "recovery_required",
        "rolled_back",
    ) or not isinstance(receipt_info, dict):
        raise u.UpgradeError("Backup is not an applied Docker upgrade")
    project_value, receipt_value = record.get("project"), receipt_info.get("path")
    if (
        not isinstance(project_value, str)
        or not Path(project_value).is_absolute()
        or not isinstance(receipt_value, str)
        or not Path(receipt_value).is_absolute()
    ):
        raise u.UpgradeError("Docker backup requires bound absolute paths")
    project = u.plain_path(Path(project_value))
    _check_rollback_code(backup, record, project)
    receipt = _receipt_path(project, ROOT, Path(receipt_value))
    before_sha, after_sha = receipt_info.get("before_sha256"), receipt_info.get("after_sha256")
    if (
        any(
            value is not None
            and (not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None)
            for value in (before_sha, after_sha)
        )
        or after_sha is None
    ):
        raise u.UpgradeError("Invalid Docker receipt checksum")
    previous = None
    if before_sha is not None:
        previous = u.plain_path(backup / "receipt-before.json").read_bytes()
        if u.digest(previous, raw=True) != before_sha:
            raise u.UpgradeError("Docker receipt backup checksum mismatch")
        meta = receipt_info.get("metadata")
        if (
            not isinstance(meta, dict)
            or any(not isinstance(meta.get(key), int) for key in ("mode", "uid", "gid"))
            or not 0 <= meta["mode"] <= 0o7777
            or meta["uid"] < 0
            or meta["gid"] < 0
        ):
            raise u.UpgradeError("Invalid Docker receipt metadata")
    marker = (
        json.dumps({"status": "rollback_pending", "release_id": record.get("release_id")}) + "\n"
    ).encode()
    current = u.digest(receipt.read_bytes(), raw=True) if receipt.is_file() else None
    if current not in {before_sha, after_sha, u.digest(marker, raw=True)}:
        raise u.UpgradeError("Docker receipt changed after upgrade")
    u.atomic_write(receipt, marker)
    if record["status"] != "rolled_back":
        project = u.rollback(backup)
    if previous is None:
        receipt.unlink()
    else:
        u.atomic_write(receipt, previous, receipt_info["metadata"])
    return project


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "apply"):
        command = commands.add_parser(name)
        command.add_argument("--project", required=True, type=Path)
        command.add_argument("--package", default=ROOT, type=Path)
        command.add_argument("--backup-dir", required=True)
        command.add_argument("--receipt", type=Path)
    commands.add_parser("rollback").add_argument("--backup", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "check":
            _, changes = preflight(args.project, args.package)
            u.backup_root(u.plain_path(args.project), u.plain_path(args.package), args.backup_dir)
            if args.receipt is not None:
                _receipt_path(u.plain_path(args.project), u.plain_path(args.package), args.receipt)
            print(
                f"Original Docker runtime and all payload checks passed; {len(changes)} runtime files require changes."
            )
        elif args.command == "apply":
            backup = apply(args.project, args.backup_dir, args.package, args.receipt)
            print(
                f"Docker runtime patch applied. Backup: {backup}"
                if backup
                else "Docker runtime already patched; no code files changed."
            )
        else:
            print(f"Docker runtime patch rolled back: {rollback(args.backup)}")
        return 0
    except (u.UpgradeError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Docker patch refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
