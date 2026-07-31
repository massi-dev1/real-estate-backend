"""Check that a container's memory limit covers concurrent Argon2id hashing.

Audit finding PYT-01. Argon2id is memory-hard *by design* — the 64 MiB working
set is what prices up an offline crack of a stolen hash — so the parameters are
not the thing to tune. What has to hold instead is a deployment invariant:

    WEB_CONCURRENCY * ARGON2_MEMORY_MIB + baseline  <=  container memory limit

Each uvicorn worker process hashes one password at a time (Argon2 is CPU-bound
and runs inline in the event loop), so the process count — not request
concurrency — bounds how many 64 MiB buffers can be live at once. Exceeding the
limit does not degrade gracefully: the kernel OOM-kills the container mid-login.

Run it against the limit you intend to deploy with:

    uv run python scripts/check_argon2_budget.py --limit-mib 512 --web-concurrency 2

With no ``--limit-mib`` it reads the *current* container's cgroup limit (v2 then
v1), which is what makes it useful as a pre-flight check inside the image:

    docker run --rm --memory=512m real-estate-backend:latest \\
        python scripts/check_argon2_budget.py

Exits non-zero when the budget does not fit, so it can gate a deploy.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.core.security import ARGON2_MEMORY_MIB, ARGON2_PARALLELISM, ARGON2_TIME_COST

# Resident set of the app itself (interpreter, SQLAlchemy, FastAPI, the loaded
# module graph) before any hashing, per uvicorn worker process. Measured on the
# Part 25 image; deliberately rounded up — a budget check that is optimistic
# about baseline is worse than no check.
BASELINE_MIB_PER_WORKER = 128

_CGROUP_V2 = Path("/sys/fs/cgroup/memory.max")
_CGROUP_V1 = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")


def detect_limit_mib() -> int | None:
    """The current container's memory limit in MiB, or None if unlimited/absent."""
    for path in (_CGROUP_V2, _CGROUP_V1):
        try:
            raw = path.read_text().strip()
        except OSError:
            continue
        if raw == "max":
            return None
        try:
            value = int(raw)
        except ValueError:
            continue
        # cgroup v1 reports a sentinel near 2^63 when unlimited.
        if value >= 1 << 62:
            return None
        return value // (1024 * 1024)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit-mib",
        type=int,
        default=None,
        help="Container memory limit in MiB (default: read from cgroups).",
    )
    parser.add_argument(
        "--web-concurrency",
        type=int,
        default=2,
        help="uvicorn worker processes, i.e. concurrent hashes (default: 2, the §16 default).",
    )
    args = parser.parse_args()

    limit = args.limit_mib if args.limit_mib is not None else detect_limit_mib()
    workers = args.web_concurrency

    hashing = workers * ARGON2_MEMORY_MIB
    baseline = workers * BASELINE_MIB_PER_WORKER
    required = hashing + baseline

    print(f"Argon2id: m={ARGON2_MEMORY_MIB}MiB t={ARGON2_TIME_COST} p={ARGON2_PARALLELISM}")
    print(f"WEB_CONCURRENCY={workers}")
    print(f"  hashing peak : {hashing} MiB ({workers} * {ARGON2_MEMORY_MIB} MiB)")
    print(f"  baseline     : {baseline} MiB ({workers} * {BASELINE_MIB_PER_WORKER} MiB)")
    print(f"  required     : {required} MiB")

    if limit is None:
        print("  limit        : unlimited / not detected")
        print("\nNo memory limit to check against. Pass --limit-mib to verify a target.")
        return 0

    print(f"  limit        : {limit} MiB")
    headroom = limit - required
    if headroom < 0:
        print(
            f"\nFAIL: over budget by {-headroom} MiB. An authentication burst will "
            f"OOM-kill this container.\nEither raise the memory limit to >= {required} MiB, "
            f"or lower WEB_CONCURRENCY to {max(1, (limit - baseline) // ARGON2_MEMORY_MIB)}."
        )
        return 1

    print(f"\nOK: {headroom} MiB headroom.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
