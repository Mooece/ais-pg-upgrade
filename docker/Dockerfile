# The original project must already exist as a compatible Linux runtime image.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG PATCH_RELEASE
ARG BASE_IMAGE_ID
ARG BASE_IMAGE_REF

COPY upgrade.py patch-manifest.json /opt/ais-pg-upgrade-build/
COPY docker/apply_patch.py /opt/ais-pg-upgrade-build/docker/apply_patch.py
COPY payload/ /opt/ais-pg-upgrade-build/payload/

# Keep this stage as the result: a fresh FROM plus COPY would retain deleted assets.
RUN python3 -B /opt/ais-pg-upgrade-build/docker/apply_patch.py apply \
        --project /app \
        --backup-dir /opt/ais-pg-upgrade-build-backups \
        --receipt /opt/ais-pg-upgrade/receipt.json \
    && python3 -B /opt/ais-pg-upgrade-build/docker/apply_patch.py check \
        --project /app --backup-dir /opt/ais-pg-upgrade-build-backups \
    && python3 -B -c "from pathlib import Path; import shutil; p=Path('/opt/ais-pg-upgrade-build-backups'); shutil.rmtree(p) if p.exists() else None; shutil.rmtree('/opt/ais-pg-upgrade-build')"

LABEL io.github.mooece.ais-pg-upgrade.release="${PATCH_RELEASE}" \
      io.github.mooece.ais-pg-upgrade.base-image-id="${BASE_IMAGE_ID}" \
      io.github.mooece.ais-pg-upgrade.base-image-ref="${BASE_IMAGE_REF}" \
      io.github.mooece.ais-pg-upgrade.source="https://github.com/Mooece/ais-pg-upgrade"

# USER, ENTRYPOINT, CMD, ENV, volumes, ports and healthcheck come from the base.
