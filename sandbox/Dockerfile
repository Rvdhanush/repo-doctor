# repo-doctor sandbox — the environment the agent operates inside.
#
# Deliberately LEAN. We install git + certs and nothing else: no build-essential,
# no libgl1, no ffmpeg. If this image pre-installed every system library, SPEC.md
# section 6 Scenario C ("pip install succeeds but import fails on a missing shared
# object") could never reproduce, and the agent would have no real system-dependency
# failures to diagnose. Resist the urge to add packages here — installing them is
# the agent's job, not the image's.
FROM python:3.11-slim-bookworm

# ca-certificates is required for HTTPS clone/pip. curl is a diagnostic convenience.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        curl \
 && rm -rf /var/lib/apt/lists/*

# Installs land in a venv rather than the system interpreter, so a future
# "reset the environment" (increment 2) is a single directory delete.
ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Quieter, more deterministic pip behaviour. Individual invocations still pass
# their own flags; these are the defaults for anything the agent runs later.
ENV PIP_NO_INPUT=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_COLOR=1 \
    PIP_DEFAULT_TIMEOUT=60 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /work

# The container is kept alive so the harness can `docker exec` a sequence of
# commands against one persistent filesystem. Increment 2's fix loop depends on
# state surviving between commands.
CMD ["sleep", "infinity"]
