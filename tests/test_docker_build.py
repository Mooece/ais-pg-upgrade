"""Docker build orchestration keeps the installed image and its runtime contract."""

import copy
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

import upgrade as u

_SPEC = importlib.util.spec_from_file_location(
    "docker_build_under_test", Path(__file__).resolve().parents[1] / "docker" / "build.py"
)
builder = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(builder)

BASE_ID = "sha256:" + "a" * 64
RESULT_ID = "sha256:" + "b" * 64
BASE_REF = "example/original:fixed"
BASE_DIGEST = "example/original@sha256:" + "c" * 64
BASE_LOCAL_PIN = "ais-pg-upgrade-base:" + BASE_ID.removeprefix("sha256:")
OUTPUT_TAG = "example/patched:verified"
OWN_PREFIX = "io.github.mooece.ais-pg-upgrade."


def image(image_id=BASE_ID, *, digests=None):
    return {
        "Id": image_id,
        "Os": "linux",
        "Architecture": "amd64",
        "RepoTags": [BASE_REF, "example/original:alias"],
        "RepoDigests": [BASE_DIGEST] if digests is None else digests,
        "Config": {
            "Entrypoint": ["/app/docker-entrypoint.sh"],
            "Cmd": ["python3", "main.py", "server", "--port", "8080"],
            "WorkingDir": "/app",
            "User": "",
            "Env": ["AISTUDIO_DATA_DIR=/app/data"],
            "ExposedPorts": {"8080/tcp": {}},
            "Healthcheck": {"Test": ["CMD", "curl", "-f", "http://localhost:8080/health"]},
            "Volumes": {"/app/data": {}},
            "Labels": {
                "org.opencontainers.image.revision": u.BASE,
                "org.opencontainers.image.source": u.UPSTREAM,
            },
        },
    }


@pytest.fixture
def build_env(tmp_path, monkeypatch):
    package = tmp_path / "addon"
    package.mkdir()
    data = b"# synthetic runtime delta\n"
    path = package / "payload/src/aistudio_api/example.py"
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    (package / "patch-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_id": "test-release",
                "upstream": {"url": u.UPSTREAM, "commit": u.BASE},
                "entries": [
                    {
                        "path": "src/aistudio_api/example.py",
                        "action": "write",
                        "base_sha256": None,
                        "payload_sha256": u.digest(data),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(builder, "ROOT", package)
    base = image()
    result = copy.deepcopy(base)
    result["Id"] = RESULT_ID
    result["RepoTags"] = [OUTPUT_TAG]
    result["RepoDigests"] = []
    result["Config"]["Labels"].update(
        {
            OWN_PREFIX + "release": "test-release",
            OWN_PREFIX + "base-image-id": BASE_ID,
            OWN_PREFIX + "base-image-ref": BASE_LOCAL_PIN,
            OWN_PREFIX + "source": "https://github.com/Mooece/ais-pg-upgrade",
        }
    )
    calls = []
    built_refs = set()

    def command(args, **kwargs):
        calls.append((list(args), kwargs))
        assert isinstance(args, list) and all(isinstance(arg, str) for arg in args)
        assert kwargs.get("shell") is not True
        if args[:2] == ["docker", "build"]:
            built_refs.add(args[args.index("--tag") + 1])
        if args[:3] == ["docker", "image", "inspect"]:
            value = result if args[-1] == OUTPUT_TAG or args[-1] in built_refs else base
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps([value]), stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(builder.subprocess, "run", command)
    return package, base, result, calls


def builds(calls):
    return [args for args, _ in calls if args[:2] == ["docker", "build"]]


@pytest.mark.parametrize(
    "reference,canonical",
    [
        ("base", "docker.io/library/base:latest"),
        ("base:fixed", "docker.io/library/base:fixed"),
        ("docker.io/base:fixed", "docker.io/library/base:fixed"),
        ("index.docker.io/library/base:fixed", "docker.io/library/base:fixed"),
        ("team/base", "docker.io/team/base:latest"),
        ("docker.io/team/base", "docker.io/team/base:latest"),
        ("index.docker.io/team/base:fixed", "docker.io/team/base:fixed"),
        ("localhost/base", "localhost/base:latest"),
        ("localhost:5000/team/base", "localhost:5000/team/base:latest"),
        ("registry.example:5000/team/base:fixed", "registry.example:5000/team/base:fixed"),
        ("ghcr.io/mooece/base:fixed", "ghcr.io/mooece/base:fixed"),
        (BASE_DIGEST, BASE_DIGEST),
    ],
)
def test_tag_normalization_preserves_namespaces_ports_and_digest_refs(reference, canonical):
    assert builder.normalized_tag(reference) == canonical


@pytest.mark.parametrize(
    "base_ref,tag", [("", OUTPUT_TAG), ("--pull", OUTPUT_TAG), (BASE_REF, ""), (BASE_REF, "--push")]
)
def test_build_requires_explicit_original_and_new_image_tags(build_env, base_ref, tag):
    _, _, _, calls = build_env
    with pytest.raises(ValueError, match="base-image"):
        builder.build(base_ref, tag)
    assert not calls


@pytest.mark.parametrize("payload", [[], [{}, {}], {}, "invalid shape"])
def test_inspection_requires_exactly_one_installed_image(monkeypatch, payload):
    monkeypatch.setattr(
        builder.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload)),
    )
    with pytest.raises(ValueError, match="one installed image"):
        builder.inspect_image(BASE_REF)


@pytest.mark.parametrize(
    "property_name,value", [("Os", "windows"), ("Id", None), ("Id", "unverified")]
)
def test_non_linux_or_unidentified_images_are_refused(build_env, property_name, value):
    _, base, _, calls = build_env
    base[property_name] = value
    with pytest.raises(ValueError):
        builder.build(BASE_REF, OUTPUT_TAG)
    assert not builds(calls)


def test_missing_original_image_is_not_automatically_installed(build_env, monkeypatch):
    _, _, _, calls = build_env

    def missing(args, **kwargs):
        calls.append((args, kwargs))
        raise subprocess.CalledProcessError(1, args, stderr="No such image")

    monkeypatch.setattr(builder.subprocess, "run", missing)
    with pytest.raises(subprocess.CalledProcessError):
        builder.build(BASE_REF, OUTPUT_TAG)
    assert len(calls) == 1 and calls[0][0][:3] == ["docker", "image", "inspect"]


def test_unsupported_upstream_revision_is_refused_before_build(build_env):
    _, base, _, calls = build_env
    base["Config"]["Labels"]["org.opencontainers.image.revision"] = "d" * 40
    with pytest.raises(ValueError, match="revision is unsupported"):
        builder.build(BASE_REF, OUTPUT_TAG)
    assert not builds(calls)


def test_registry_tag_is_pinned_to_installed_id_and_reported(build_env, tmp_path):
    package, _, _, calls = build_env
    report = tmp_path / "reports/build.json"
    summary = builder.build(BASE_REF, OUTPUT_TAG, report)
    args = builds(calls)[0]
    assert "BASE_IMAGE=" + BASE_LOCAL_PIN in args
    assert "BASE_IMAGE=" + BASE_DIGEST not in args
    assert "BASE_IMAGE=" + BASE_REF not in args
    assert args[-1] == str(package)
    temporary = args[args.index("--tag") + 1]
    assert temporary.startswith("ais-pg-upgrade-build:") and temporary != OUTPUT_TAG
    promotion = ["docker", "image", "tag", temporary, OUTPUT_TAG]
    assert (promotion, {"check": True}) in calls
    inspected = next(
        index
        for index, (call, _) in enumerate(calls)
        if call == ["docker", "image", "inspect", temporary]
    )
    promoted = next(index for index, (call, _) in enumerate(calls) if call == promotion)
    cleaned = next(
        index
        for index, (call, _) in enumerate(calls)
        if call == ["docker", "image", "rm", temporary]
    )
    assert inspected < promoted < cleaned
    assert summary["base_image_id"] == BASE_ID
    assert summary["image_id"] == RESULT_ID
    assert summary["original_startup_preserved"] is True
    assert json.loads(report.read_text(encoding="utf-8")) == summary


def test_explicit_digest_does_not_retag_original_image(build_env):
    _, base, _, calls = build_env
    assert builder.pin_base(BASE_DIGEST, base) == BASE_DIGEST
    assert not calls


def test_explicit_registry_digest_remains_the_build_base(build_env):
    _, _, result, calls = build_env
    result["Config"]["Labels"][OWN_PREFIX + "base-image-ref"] = BASE_DIGEST
    summary = builder.build(BASE_DIGEST, OUTPUT_TAG)
    assert "BASE_IMAGE=" + BASE_DIGEST in builds(calls)[0]
    assert not any(
        call[:3] == ["docker", "image", "tag"] and call[-1] == BASE_LOCAL_PIN for call, _ in calls
    )
    assert summary["base_image"] == BASE_DIGEST


def test_containerd_unpublished_digest_is_not_used_as_a_registry_base(build_env):
    _, base, _, calls = build_env
    unpublished = "docker.io/library/unpublished-local-image@sha256:" + "d" * 64
    base["RepoDigests"] = [unpublished]
    summary = builder.build(BASE_REF, OUTPUT_TAG)
    assert "BASE_IMAGE=" + BASE_LOCAL_PIN in builds(calls)[0]
    assert not any(unpublished in argument for call, _ in calls for argument in call)
    assert summary["base_image_id"] == BASE_ID


def test_locally_built_original_is_pinned_by_its_full_id(build_env):
    _, base, result, calls = build_env
    base["RepoDigests"] = []
    pinned = "ais-pg-upgrade-base:" + BASE_ID.removeprefix("sha256:")
    result["Config"]["Labels"][OWN_PREFIX + "base-image-ref"] = pinned
    summary = builder.build(BASE_REF, OUTPUT_TAG)
    assert (["docker", "image", "tag", BASE_ID, pinned], {"check": True}) in calls
    assert "BASE_IMAGE=" + pinned in builds(calls)[0]
    assert summary["base_image"] == pinned


@pytest.mark.parametrize("key", builder.CONFIG_KEYS)
def test_original_startup_configuration_cannot_silently_change(build_env, key):
    _, base, result, calls = build_env
    result["Config"][key] = "changed"
    with pytest.raises(ValueError, match=key):
        builder.build(BASE_REF, OUTPUT_TAG)
    assert len(builds(calls)) == 1
    assert not any(
        call[:3] == ["docker", "image", "tag"] and call[-1] == OUTPUT_TAG for call, _ in calls
    )
    assert base["Id"] == BASE_ID


def test_original_labels_are_preserved(build_env):
    _, _, result, _ = build_env
    result["Config"]["Labels"]["org.opencontainers.image.source"] = "other repository"
    with pytest.raises(ValueError, match="original label"):
        builder.build(BASE_REF, OUTPUT_TAG)


def test_earlier_patch_metadata_can_be_updated_without_changing_original_metadata(build_env):
    _, base, result, _ = build_env
    base["Config"]["Labels"][OWN_PREFIX + "release"] = "previous-patch"
    builder.verify_inherited_config(base, result)


def test_report_inside_patch_repository_is_refused_before_docker(build_env):
    package, _, _, calls = build_env
    with pytest.raises(ValueError, match="outside"):
        builder.build(BASE_REF, OUTPUT_TAG, package / "reports/build.json")
    assert not calls


def test_pinned_base_identity_change_during_build_is_refused(build_env, monkeypatch, tmp_path):
    _, base, result, calls = build_env
    original = builder.subprocess.run

    def race(args, **kwargs):
        if args[:3] == ["docker", "image", "inspect"] and args[-1] == BASE_LOCAL_PIN:
            changed = copy.deepcopy(base)
            changed["Id"] = "sha256:" + "e" * 64
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps([changed]))
        return original(args, **kwargs)

    monkeypatch.setattr(builder.subprocess, "run", race)
    report = tmp_path / "build.json"
    with pytest.raises(ValueError, match="changed during"):
        builder.build(BASE_REF, OUTPUT_TAG, report)
    assert result["Id"] == RESULT_ID and not report.exists()


@pytest.mark.parametrize(
    "base_ref,tag",
    [
        (BASE_REF, BASE_REF),
        (BASE_REF, "example/original:alias"),
        ("example/original", "example/original:latest"),
        (BASE_REF, BASE_DIGEST),
        (BASE_REF, BASE_LOCAL_PIN),
    ],
)
def test_output_tag_never_overwrites_an_original_image_tag(build_env, base_ref, tag):
    _, _, _, calls = build_env
    with pytest.raises(ValueError, match="original|new"):
        builder.build(base_ref, tag)
    assert not builds(calls)


@pytest.mark.parametrize(
    "base_ref,tag",
    [
        ("base:fixed", "docker.io/library/base:fixed"),
        ("base:fixed", "docker.io/base:fixed"),
        ("base:fixed", "index.docker.io/library/base:fixed"),
        ("base:fixed", "index.docker.io/base:fixed"),
        ("base", "docker.io/library/base:latest"),
        (BASE_REF, "docker.io/library/" + BASE_LOCAL_PIN),
        (BASE_REF, "docker.io/" + BASE_LOCAL_PIN),
        (BASE_REF, "index.docker.io/library/" + BASE_LOCAL_PIN),
        (BASE_REF, "index.docker.io/" + BASE_LOCAL_PIN),
    ],
)
def test_registry_aliases_cannot_overwrite_original_or_generated_base_pin(build_env, base_ref, tag):
    _, _, _, calls = build_env
    with pytest.raises(ValueError, match="original|new|separate"):
        builder.build(base_ref, tag)
    assert not builds(calls)


@pytest.mark.parametrize("suffix", ["release", "base-image-id", "base-image-ref", "source"])
def test_success_requires_correct_patch_identity_labels(build_env, suffix):
    _, _, result, _ = build_env
    result["Config"]["Labels"][OWN_PREFIX + suffix] = "incorrect"
    with pytest.raises(ValueError, match="label|identity|metadata"):
        builder.build(BASE_REF, OUTPUT_TAG)


def test_corrupt_payload_is_rejected_before_context_upload(build_env):
    package, _, _, calls = build_env
    (package / "payload/src/aistudio_api/example.py").write_bytes(b"unverified edit")
    with pytest.raises((ValueError, u.UpgradeError), match="payload|checksum|Corrupt"):
        builder.build(BASE_REF, OUTPUT_TAG)
    assert not builds(calls)


def test_unmanaged_payload_is_rejected_before_context_upload(build_env):
    package, _, _, calls = build_env
    (package / "payload/private-cookie.json").write_bytes(b"synthetic private sentinel")
    with pytest.raises((ValueError, u.UpgradeError), match="payload|Unmanaged|unmanaged"):
        builder.build(BASE_REF, OUTPUT_TAG)
    assert not builds(calls)


def test_failed_result_verification_does_not_replace_previous_patch_tag(build_env, monkeypatch):
    _, base, result, calls = build_env
    previous = copy.deepcopy(result)
    previous["Id"] = "sha256:" + "f" * 64
    result["Config"]["Labels"][OWN_PREFIX + "release"] = "invalid-result"
    registry = {BASE_ID: base, BASE_REF: base, BASE_DIGEST: base, OUTPUT_TAG: previous}

    def docker(args, **kwargs):
        calls.append((args, kwargs))
        if args[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps([registry[args[-1]]]))
        if args[:2] == ["docker", "build"]:
            registry[args[args.index("--tag") + 1]] = copy.deepcopy(result)
        elif args[:3] == ["docker", "image", "tag"]:
            registry[args[-1]] = registry[args[-2]]
        elif args[:3] == ["docker", "image", "rm"]:
            registry.pop(args[-1], None)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(builder.subprocess, "run", docker)
    with pytest.raises(ValueError, match="metadata|label|identity"):
        builder.build(BASE_REF, OUTPUT_TAG)
    assert registry[OUTPUT_TAG]["Id"] == previous["Id"]
    assert registry[BASE_REF]["Id"] == BASE_ID
    assert not any(tag.startswith("ais-pg-upgrade-build:") for tag in registry)


def test_docker_context_is_opt_in_and_startup_directives_are_inherited():
    repository = Path(__file__).resolve().parents[1]
    rules = [
        line.strip()
        for line in (repository / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert rules[0] == "**"
    assert not any(rule in ("!**", "!.", "!.git", "!.env", "!data", "!config") for rule in rules)
    instructions = [
        line.split()[0].upper()
        for line in (repository / "docker/Dockerfile").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert not set(instructions) & {
        "ENTRYPOINT",
        "CMD",
        "USER",
        "ENV",
        "VOLUME",
        "EXPOSE",
        "HEALTHCHECK",
    }
