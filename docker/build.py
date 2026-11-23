"""Build a checked derivative of a separately installed original Docker image."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "docker"))
import apply_patch as image_patch

import upgrade as u

CONFIG_KEYS = (
    "Entrypoint",
    "Cmd",
    "WorkingDir",
    "User",
    "Env",
    "ExposedPorts",
    "Healthcheck",
    "Volumes",
)
LABEL_PREFIX = "io.github.mooece.ais-pg-upgrade."


def normalized_tag(reference: str) -> str:
    if "@" in reference:
        return reference
    if ":" in reference.rsplit("/", 1)[-1]:
        name, tag = reference.rsplit(":", 1)
    else:
        name, tag = reference, "latest"
    parts = name.split("/")
    first = parts[0]
    if len(parts) > 1 and ("." in first or ":" in first or first == "localhost"):
        registry, path = first, parts[1:]
    else:
        registry, path = "docker.io", parts
    if registry == "index.docker.io":
        registry = "docker.io"
    if registry == "docker.io" and len(path) == 1:
        path.insert(0, "library")
    return registry + "/" + "/".join(path) + ":" + tag


def inspect_image(reference: str) -> dict:
    result = subprocess.run(
        ["docker", "image", "inspect", reference],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    images = json.loads(result.stdout)
    if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
        raise ValueError("Docker did not return one installed image")
    image = images[0]
    if image.get("Os") != "linux":
        raise ValueError("The patch Dockerfile requires a Linux original image")
    image_id = image.get("Id")
    if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
        raise ValueError("Docker returned an invalid original image ID")
    return image


def pin_base(reference: str, image: dict) -> str:
    if "@sha256:" in reference:
        return reference
    # Containerd also assigns RepoDigests to images that were never published.
    # Those references cannot be pulled by BuildKit. Pin every local/tag input
    # to its installed ID, without assuming that a registry has that digest.
    pinned = "ais-pg-upgrade-base:" + image["Id"].removeprefix("sha256:")
    subprocess.run(["docker", "image", "tag", image["Id"], pinned], check=True)
    return pinned


def verify_inherited_config(base: dict, result: dict) -> None:
    before, after = base.get("Config"), result.get("Config")
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise TypeError("Docker image configuration is missing")
    for key in CONFIG_KEYS:
        if before.get(key) != after.get(key):
            raise ValueError(f"Patched image changed original startup configuration: {key}")
    for key, value in (before.get("Labels") or {}).items():
        if not key.startswith(LABEL_PREFIX) and (after.get("Labels") or {}).get(key) != value:
            raise ValueError(f"Patched image changed original label: {key}")


def build(base_image: str, tag: str, report: Path | None = None) -> dict:
    if not base_image or base_image.startswith("-") or not tag or tag.startswith("-"):
        raise ValueError("Supply an existing original --base-image and a new --tag")
    if report is not None:
        report = u.plain_path(report)
        if report.is_relative_to(ROOT) or report == ROOT:
            raise ValueError("Build reports must be outside the patch repository")
    manifest = image_patch.validate_package(ROOT)
    base = inspect_image(base_image)
    if "@" in tag or normalized_tag(tag) in {
        normalized_tag(reference) for reference in [base_image, *(base.get("RepoTags") or [])]
    }:
        raise ValueError("Use a new output tag; the installed original image tag must be preserved")
    config = base.get("Config") or {}
    revision = (config.get("Labels") or {}).get("org.opencontainers.image.revision")
    if revision is not None and revision != u.BASE:
        raise ValueError(f"Original image revision is unsupported; expected {u.BASE}")
    pinned = pin_base(base_image, base)
    if normalized_tag(tag) == normalized_tag(pinned):
        raise ValueError("Use a separate output tag; the pinned original image must be preserved")
    temporary = "ais-pg-upgrade-build:" + uuid.uuid4().hex
    try:
        subprocess.run(
            [
                "docker",
                "build",
                "--progress=plain",
                "--file",
                str(ROOT / "docker/Dockerfile"),
                "--build-arg",
                f"BASE_IMAGE={pinned}",
                "--build-arg",
                f"PATCH_RELEASE={manifest['release_id']}",
                "--build-arg",
                f"BASE_IMAGE_ID={base['Id']}",
                "--build-arg",
                f"BASE_IMAGE_REF={pinned}",
                "--tag",
                temporary,
                str(ROOT),
            ],
            check=True,
        )
        result = inspect_image(temporary)
        verify_inherited_config(base, result)
        expected_labels = {
            "release": manifest["release_id"],
            "base-image-id": base["Id"],
            "base-image-ref": pinned,
            "source": "https://github.com/Mooece/ais-pg-upgrade",
        }
        labels = (result.get("Config") or {}).get("Labels") or {}
        if any(labels.get(LABEL_PREFIX + key) != value for key, value in expected_labels.items()):
            raise ValueError(
                "Patched image release/base metadata does not match the verified build"
            )
        if inspect_image(pinned)["Id"] != base["Id"]:
            raise ValueError("Pinned original image changed during the build")
        subprocess.run(["docker", "image", "tag", temporary, tag], check=True)
    finally:
        # Remove only this build's temporary reference. The promoted output,
        # original image and any previous valid output remain available.
        subprocess.run(
            ["docker", "image", "rm", temporary],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    summary = {
        "image": tag,
        "image_id": result["Id"],
        "base_image": pinned,
        "base_image_id": base["Id"],
        "architecture": result.get("Architecture"),
        "release_id": manifest["release_id"],
        "upstream_commit": u.BASE,
        "original_startup_preserved": True,
    }
    if report is not None:
        report.parent.mkdir(parents=True, exist_ok=True)
        u.atomic_write(report, (json.dumps(summary, ensure_ascii=False, indent=2) + "\n").encode())
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-image", required=True, help="Already pulled/built compatible original image"
    )
    parser.add_argument("--tag", required=True, help="Tag for the new patched image")
    parser.add_argument(
        "--report", type=Path, help="Optional verification report outside this repository"
    )
    args = parser.parse_args()
    try:
        print(
            json.dumps(build(args.base_image, args.tag, args.report), ensure_ascii=False, indent=2)
        )
        return 0
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        u.UpgradeError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"Docker patch build refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
