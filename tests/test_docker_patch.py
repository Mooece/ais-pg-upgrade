"""Real file behavior of the image adapter, with only native process probes simulated."""

import json
import sys
from types import SimpleNamespace

import pytest

import upgrade as u
from docker import apply_patch as d


@pytest.fixture
def image(tmp_path, monkeypatch):
    project, package, backups = tmp_path / "app", tmp_path / "patch", tmp_path / "backups"
    project.mkdir()
    package.mkdir()
    for relative in (*d.MARKERS, "waa-signer/bin/waa-signer"):
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"original\r\n")
    for relative in (".env", "config.yaml", "data/cookies.json", "data/records.sqlite"):
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic protected data\r\n")
    monkeypatch.setattr(
        d.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout='{"ok":true,"mode":"ipc"}', stderr=""
        ),
    )
    return project, package, backups, tmp_path / "state/receipt.json"


def entry(
    project,
    package,
    relative="src/aistudio_api/domain/models.py",
    before=b"original\n",
    after=b"patched\n",
):
    if before is not None:
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(before)
    if after is not None:
        payload = package / "payload" / relative
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_bytes(after)
    return {
        "path": relative,
        "action": "write" if after is not None else "delete",
        "base_sha256": u.digest(before) if before is not None else None,
        "payload_sha256": u.digest(after) if after is not None else None,
    }


def manifest(package, entries):
    value = {
        "schema_version": 1,
        "release_id": "docker-test",
        "upstream": {"url": u.UPSTREAM, "commit": u.BASE},
        "entries": entries,
    }
    (package / "patch-manifest.json").write_text(json.dumps(value), encoding="utf-8")
    return value


def test_package_validation_needs_no_original_image_or_process_probe(tmp_path, monkeypatch):
    project, package = tmp_path / "missing-original", tmp_path / "patch"
    package.mkdir()
    changes = [
        entry(project, package, before=None),
        entry(project, package, "web/index.html", before=None),
    ]
    expected = manifest(package, changes)

    def unexpected(*args, **kwargs):
        raise AssertionError("Package validation must run before any image probe")

    monkeypatch.setattr(d, "installed", unexpected)
    monkeypatch.setattr(d.subprocess, "run", unexpected)
    assert d.validate_package(package) == expected
    assert not project.exists()


@pytest.mark.parametrize("extra", ["private-capture.json", ".env", "cookies/session.json"])
def test_package_validation_refuses_extra_private_files_without_docker(
    tmp_path, monkeypatch, extra
):
    package = tmp_path / "patch"
    package.mkdir()
    manifest(package, [entry(tmp_path / "missing-original", package, before=None)])
    unwanted = package / "payload" / extra
    unwanted.parent.mkdir(parents=True, exist_ok=True)
    unwanted.write_bytes(b"synthetic private content")

    def unexpected(*args, **kwargs):
        raise AssertionError("Private payload must be rejected before Docker/image probes")

    monkeypatch.setattr(d, "installed", unexpected)
    monkeypatch.setattr(d.subprocess, "run", unexpected)
    with pytest.raises(u.UpgradeError, match="unmanaged"):
        d.validate_package(package)
    assert unwanted.read_bytes() == b"synthetic private content"


def test_runtime_only_apply_repeat_and_rollback_preserve_original_data(image):
    project, package, backups, receipt = image
    changes = [
        entry(project, package, before=b"original\r\n"),
        entry(project, package, "src/aistudio_api/api/new.py", before=None),
        entry(project, package, "src/aistudio_api/static/obsolete.js", after=None),
        entry(project, package, "tests/unit/upstream.py", before=None),
        entry(project, package, "web/index.html", before=None),
    ]
    # Nonruntime modified upstream files need not be installed in an official runtime.
    changes[3]["base_sha256"] = u.digest(b"upstream tests")
    changes[4]["base_sha256"] = u.digest(b"upstream web source")
    manifest(package, changes)
    protected = {
        relative: (project / relative).read_bytes()
        for relative in (".env", "config.yaml", "data/cookies.json", "data/records.sqlite")
    }
    target = project / changes[0]["path"]
    target.chmod(0o640)
    mode = target.stat().st_mode
    _, plan = d.preflight(project, package)
    assert len(plan) == 3 and not backups.exists()
    backup = d.apply(project, str(backups), package, receipt)
    assert target.read_bytes() == b"patched\n" and target.stat().st_mode == mode
    assert (project / changes[1]["path"]).is_file()
    assert not (project / changes[2]["path"]).exists()
    assert not (project / "tests").exists() and not (project / "web").exists()
    assert json.loads(receipt.read_text())["status"] == "applied"
    assert json.loads(receipt.read_text())["runtime_writes"] == 2
    assert d.apply(project, str(backups), package, receipt) is None
    assert len(list(backups.iterdir())) == 1
    assert d.rollback(backup) == project
    assert target.read_bytes() == b"original\r\n" and target.stat().st_mode == mode
    assert not (project / changes[1]["path"]).exists()
    assert (project / changes[2]["path"]).read_bytes() == b"original\n"
    assert not receipt.exists()
    assert all(
        (project / relative).read_bytes() == content for relative, content in protected.items()
    )


def test_previous_patched_image_hash_and_receipt_are_restored(image):
    project, package, backups, receipt = image
    change = entry(project, package, before=b"older patch\r\n")
    change["base_sha256"] = u.digest(b"upstream base\n")
    change["accepted_sha256"] = [u.digest(b"older patch\n")]
    manifest(package, [change])
    receipt.parent.mkdir()
    original_receipt = b'{"status":"applied","release_id":"older"}\r\n'
    receipt.write_bytes(original_receipt)
    backup = d.apply(project, str(backups), package, receipt)
    assert json.loads(receipt.read_text())["release_id"] == "docker-test"
    d.rollback(backup)
    assert (project / change["path"]).read_bytes() == b"older patch\r\n"
    assert receipt.read_bytes() == original_receipt


def test_unknown_image_content_reports_supported_baseline(image):
    project, package, backups, receipt = image
    change = entry(project, package, "src/aistudio_api/api/schemas.py")
    manifest(package, [change])
    target = project / change["path"]
    current = b"new upstream content\r\n"
    target.write_bytes(current)
    with pytest.raises(u.UpgradeError, match="Unknown target changes") as error:
        d.apply(project, str(backups), package, receipt)
    assert f"Supported upstream baseline: {u.BASE}" in str(error.value)
    assert f"Target SHA256 (LF-normalized): {u.digest(current)}" in str(error.value)
    assert target.read_bytes() == current
    assert not backups.exists() and not receipt.exists()


def test_uses_real_global_interpreter_and_signer_without_fake_source_install(image, monkeypatch):
    project, package, _, _ = image
    manifest(package, [entry(project, package)])
    calls = []

    def probe(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"ok":true,"mode":"ipc"}', stderr="")

    monkeypatch.setattr(d.subprocess, "run", probe)
    d.preflight(project, package)
    assert len(calls) == 2
    assert calls[0][0][0] == sys.executable and calls[0][0][1:3] == ["-I", "-B"]
    assert calls[1][0] == [str(project / "waa-signer/bin/waa-signer"), "-check"]
    assert calls[0][1]["env"]["PYTHON_DOTENV_DISABLED"] == "1"
    assert not (project / ".venv").exists() and not (project / "pyproject.toml").exists()
    with pytest.raises(u.UpgradeError, match="missing pyproject.toml"):
        u.preflight(project, package)


@pytest.mark.parametrize(
    "missing", ["main.py", "src/aistudio_api/main.py", "waa-signer/bin/waa-signer"]
)
def test_original_image_must_already_be_installed(image, missing):
    project, package, backups, receipt = image
    manifest(package, [entry(project, package)])
    (project / missing).unlink()
    with pytest.raises(u.UpgradeError, match="original|Original"):
        d.apply(project, str(backups), package, receipt)
    assert not backups.exists() and not receipt.exists()


@pytest.mark.parametrize(
    "output,code",
    [
        ("{}", 0),
        ('{"ok":true,"mode":"http"}', 0),
        ('{"ok":true,"mode":"ipc"}', 1),
        ("not json", 0),
        ("[]", 0),
    ],
)
def test_incompatible_signer_is_refused_without_echoing_output(image, monkeypatch, output, code):
    project, package, backups, receipt = image
    manifest(package, [entry(project, package)])
    monkeypatch.setattr(
        d.subprocess,
        "run",
        lambda args, **kwargs: SimpleNamespace(
            returncode=code if args[-1] == "-check" else 0, stdout=output, stderr="private error"
        ),
    )
    with pytest.raises(u.UpgradeError) as error:
        d.apply(project, str(backups), package, receipt)
    assert "private error" not in str(error.value)
    assert not backups.exists()


def test_dependency_failure_is_refused_before_backup(image, monkeypatch):
    project, package, backups, receipt = image
    manifest(package, [entry(project, package)])
    monkeypatch.setattr(
        d.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="secret", stderr="secret"),
    )
    with pytest.raises(u.UpgradeError, match="dependency") as error:
        d.apply(project, str(backups), package, receipt)
    assert "secret" not in str(error.value) and not backups.exists()


@pytest.mark.parametrize(
    "failure",
    [
        "unknown",
        "corrupt_runtime",
        "corrupt_skipped",
        "missing_skipped",
        "unsafe_skipped",
        "duplicate",
        "bad_checksum",
        "unmanaged",
        "wrong_baseline",
        "no_runtime",
    ],
)
def test_entire_payload_and_all_conflicts_checked_before_any_mutation(image, failure):
    project, package, backups, receipt = image
    runtime = entry(project, package)
    skipped = entry(project, package, "web/index.html", before=None)
    values = [runtime, skipped]
    if failure == "unknown":
        (project / runtime["path"]).write_bytes(b"unknown user modification")
    elif failure == "corrupt_runtime":
        (package / "payload" / runtime["path"]).write_bytes(b"corrupt")
    elif failure == "corrupt_skipped":
        (package / "payload" / skipped["path"]).write_bytes(b"corrupt")
    elif failure == "missing_skipped":
        (package / "payload" / skipped["path"]).unlink()
    elif failure == "unsafe_skipped":
        values.append(
            {
                "path": "web/../../data/cookies.json",
                "action": "delete",
                "base_sha256": None,
                "payload_sha256": None,
            }
        )
    elif failure == "duplicate":
        values.append(dict(runtime))
    elif failure == "bad_checksum":
        skipped["accepted_sha256"] = ["invalid"]
    elif failure == "unmanaged":
        (package / "payload/private-capture.json").write_bytes(b"not declared")
    elif failure == "no_runtime":
        values = [skipped]
    value = manifest(package, values)
    if failure == "wrong_baseline":
        value["upstream"]["commit"] = "0" * 40
        (package / "patch-manifest.json").write_text(json.dumps(value))
    before = (project / runtime["path"]).read_bytes()
    with pytest.raises(u.UpgradeError):
        d.apply(project, str(backups), package, receipt)
    assert (project / runtime["path"]).read_bytes() == before
    assert not backups.exists() and not receipt.exists()


@pytest.mark.parametrize("internal", ["project", "package"])
def test_backups_and_receipt_must_stay_outside_image_app_and_package(image, internal):
    project, package, _, _ = image
    manifest(package, [entry(project, package)])
    inside = project if internal == "project" else package
    with pytest.raises(u.UpgradeError, match="Backup root"):
        d.apply(project, str(inside / "backups"), package)
    with pytest.raises(u.UpgradeError, match="Backup root"):
        d.apply(project, str(inside.parent / "backups"), package, inside / "receipt.json")


@pytest.mark.parametrize("failure", ["second_code", "receipt"])
def test_write_or_receipt_failure_restores_code_and_previous_receipt(image, monkeypatch, failure):
    project, package, backups, receipt = image
    changes = [
        entry(project, package),
        entry(project, package, "src/aistudio_api/api/new.py", before=None),
    ]
    manifest(package, changes)
    receipt.parent.mkdir()
    receipt.write_bytes(b"previous receipt\r\n")
    atomic_write = u.atomic_write

    def failing(path, data, metadata=None):
        if (failure == "second_code" and path == project / changes[1]["path"]) or (
            failure == "receipt" and path == receipt
        ):
            raise OSError("simulated write failure")
        return atomic_write(path, data, metadata)

    monkeypatch.setattr(u, "atomic_write", failing)
    with pytest.raises(OSError, match="simulated"):
        d.apply(project, str(backups), package, receipt)
    assert (project / changes[0]["path"]).read_bytes() == b"original\n"
    assert not (project / changes[1]["path"]).exists()
    assert receipt.read_bytes() == b"previous receipt\r\n"
    assert (
        json.loads(next(backups.iterdir()).joinpath("backup-manifest.json").read_text())["status"]
        == "rolled_back"
    )


def test_manual_rollback_failure_does_not_leave_success_receipt_and_can_retry(image, monkeypatch):
    project, package, backups, receipt = image
    changes = [entry(project, package), entry(project, package, "src/aistudio_api/api/another.py")]
    manifest(package, changes)
    backup = d.apply(project, str(backups), package, receipt)
    atomic_write = u.atomic_write

    def failing(path, data, metadata=None):
        if path == project / changes[0]["path"] and data == b"original\n":
            raise OSError("rollback unavailable")
        return atomic_write(path, data, metadata)

    monkeypatch.setattr(u, "atomic_write", failing)
    with pytest.raises(OSError, match="rollback unavailable"):
        d.rollback(backup)
    assert json.loads(receipt.read_text())["status"] == "rollback_pending"
    monkeypatch.setattr(u, "atomic_write", atomic_write)
    d.rollback(backup)
    assert not receipt.exists()
    assert all((project / value["path"]).read_bytes() == b"original\n" for value in changes)


@pytest.mark.parametrize(
    "corruption", ["receipt", "receipt_backup", "manifest", "target", "code_backup", "metadata"]
)
def test_rollback_receipt_preflight_prevents_code_mutation(image, corruption):
    project, package, backups, receipt = image
    change = entry(project, package)
    manifest(package, [change])
    receipt.parent.mkdir()
    receipt.write_bytes(b"older receipt")
    backup = d.apply(project, str(backups), package, receipt)
    if corruption == "receipt":
        receipt.write_bytes(b"subsequent user edit")
    elif corruption == "receipt_backup":
        (backup / "receipt-before.json").write_bytes(b"corrupt")
    elif corruption == "manifest":
        (backup / "backup-manifest.json").write_bytes(b"{}")
    elif corruption == "target":
        (project / change["path"]).write_bytes(b"subsequent code edit")
    elif corruption == "code_backup":
        (backup / "files" / change["path"]).write_bytes(b"corrupt original")
    else:
        record = json.loads((backup / "backup-manifest.json").read_text())
        record["entries"][0]["metadata"] = {"mode": -1, "uid": 0, "gid": 0}
        u.save_record(backup, record)
    original_receipt = receipt.read_bytes()
    original_target = (project / change["path"]).read_bytes()
    with pytest.raises(u.UpgradeError):
        d.rollback(backup)
    assert (project / change["path"]).read_bytes() == original_target
    assert receipt.read_bytes() == original_receipt


def test_completed_code_rollback_can_retry_receipt_cleanup(image, monkeypatch):
    project, package, backups, receipt = image
    change = entry(project, package)
    manifest(package, [change])
    receipt.parent.mkdir()
    receipt.write_bytes(b"older receipt")
    backup = d.apply(project, str(backups), package, receipt)
    atomic_write = u.atomic_write

    def failing(path, data, metadata=None):
        if path == receipt and data == b"older receipt":
            raise OSError("receipt restore unavailable")
        return atomic_write(path, data, metadata)

    monkeypatch.setattr(u, "atomic_write", failing)
    with pytest.raises(OSError, match="receipt restore unavailable"):
        d.rollback(backup)
    assert (project / change["path"]).read_bytes() == b"original\n"
    assert json.loads(receipt.read_text())["status"] == "rollback_pending"
    monkeypatch.setattr(u, "atomic_write", atomic_write)
    d.rollback(backup)
    assert receipt.read_bytes() == b"older receipt"
