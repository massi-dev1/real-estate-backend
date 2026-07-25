"""Self-provisioning backing services for the suite (§13 testcontainers).

The suite has always required real Postgres+PostGIS, Redis, MinIO and Mailpit —
§13 is explicit that SQLite lies about JSONB/PostGIS/RLS, so there is no
in-process substitute. Until now it *assumed* a hand-started
`docker compose up`, which is fine on a warm dev box and is exactly what CI's
service containers provide, but leaves a cold checkout failing with a
connection error instead of just working.

**The reuse-a-running-stack path stays the default.** Starting four containers
costs ~20-30s per session and a docker pull on a cold cache, which is real
friction on the inner loop where the stack is already up. So:

- ``TESTCONTAINERS=1``  → provision throwaway containers on random ports and
  point the suite at them. Use on a cold machine, or to run against a version
  matrix without touching the dev stack.
- unset (default)       → use the compose/CI stack on its fixed ports.

Either way the *same* env vars are what the app reads, so nothing downstream
of `provision()` knows which mode it is in.

Images are pinned to the same tags `docker/docker-compose.yml` uses — a suite
that passes against a different Postgres than dev runs is not evidence.
"""

import os
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path

# Same images/tags as docker/docker-compose.yml. Keep in sync: testing against
# a different Postgres than the one dev and prod run is not evidence.
POSTGRES_IMAGE = "postgis/postgis:16-3.4"
REDIS_IMAGE = "redis:7-alpine"
MINIO_IMAGE = "minio/minio:latest"
MAILPIT_IMAGE = "axllent/mailpit:v1.30.5"

INITDB_SQL = Path(__file__).resolve().parent.parent / "docker" / "initdb" / "01-app-role.sql"

TEST_BUCKETS = ("media-test", "media-private-test")


def enabled() -> bool:
    """Whether to provision containers rather than reuse a running stack."""
    return os.environ.get("TESTCONTAINERS", "").lower() in {"1", "true", "yes"}


def provision(stack: ExitStack) -> None:
    """Start the backing services and pin the env vars the app reads.

    Called from ``conftest`` *before* anything imports ``app.core.config``, so
    the settings singleton picks these up like any other environment. Every
    container is registered on ``stack`` so a failure part-way through still
    tears down whatever already started.
    """
    # Ryuk is testcontainers' cleanup sidecar: it watches the session and
    # reaps containers if the process dies without unwinding. The ExitStack
    # plus the atexit hook in conftest already covers the normal exit, and
    # requiring it means pulling a *fifth* image (and failing the whole suite
    # when that one registry request is the one that times out). Opt out and
    # own the teardown.
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    _provision_postgres(stack)
    _provision_redis(stack)
    _provision_minio(stack)
    _provision_mailpit(stack)


def _provision_postgres(stack: ExitStack) -> None:
    from testcontainers.postgres import PostgresContainer

    container = stack.enter_context(
        PostgresContainer(
            POSTGRES_IMAGE,
            username="postgres",
            password="postgres",
            dbname="realestate",
        )
    )
    host = container.get_container_host_ip()
    port = container.get_exposed_port(5432)

    # The image's own entrypoint only runs initdb scripts from a mounted
    # directory; applying the *same* file compose mounts keeps one source of
    # truth for the app_user role, the test database and the PostGIS
    # extension (the script is idempotent — see its header).
    _run_initdb(container)

    os.environ["DATABASE_URL"] = (
        f"postgresql+asyncpg://app_user:app_password@{host}:{port}/realestate_test"
    )
    os.environ["DATABASE_DDL_URL"] = (
        f"postgresql+asyncpg://postgres:postgres@{host}:{port}/realestate_test"
    )


def _run_initdb(container: object) -> None:
    """Apply ``docker/initdb/01-app-role.sql`` inside the container."""
    sql = INITDB_SQL.read_text(encoding="utf-8")
    # `exec` takes no stdin, so the script rides in as a heredoc through sh.
    command = [
        "sh",
        "-c",
        f"psql -v ON_ERROR_STOP=1 -U postgres -d postgres <<'EOSQL'\n{sql}\nEOSQL",
    ]
    exit_code, output = container.exec(command)  # type: ignore[attr-defined]
    if exit_code != 0:
        raise RuntimeError(f"initdb script failed inside the container:\n{output!r}")


def _provision_redis(stack: ExitStack) -> None:
    from testcontainers.redis import RedisContainer

    container = stack.enter_context(RedisContainer(REDIS_IMAGE))
    url = f"redis://{container.get_container_host_ip()}:{container.get_exposed_port(6379)}/1"
    os.environ["REDIS_URL"] = url
    os.environ["CELERY_BROKER_URL"] = url
    os.environ["CELERY_RESULT_BACKEND"] = url


def _provision_minio(stack: ExitStack) -> None:
    from testcontainers.minio import MinioContainer

    container = stack.enter_context(
        MinioContainer(MINIO_IMAGE, access_key="minio", secret_key="minio12345")
    )
    host = container.get_container_host_ip()
    port = container.get_exposed_port(9000)
    client = container.get_client()
    for bucket in TEST_BUCKETS:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
    os.environ["STORAGE_ENDPOINT_URL"] = f"http://{host}:{port}"


def _provision_mailpit(stack: ExitStack) -> None:
    """Mailpit has no testcontainers module, so drive the generic container.

    The suite asserts on delivered mail (subjects, one-time codes), so a mail
    sink is a hard dependency, not a nicety.
    """
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy

    container = stack.enter_context(
        DockerContainer(MAILPIT_IMAGE)
        .with_exposed_ports(1025, 8025)
        # Mailpit logs this once both the SMTP listener and the HTTP API the
        # suite reads delivered mail from are up; binding earlier would race.
        .waiting_for(LogMessageWaitStrategy("accessible via"))
    )
    host = container.get_container_host_ip()
    os.environ["SMTP_HOST"] = host
    os.environ["SMTP_PORT"] = str(container.get_exposed_port(1025))
    os.environ["MAILPIT_URL"] = f"http://{host}:{container.get_exposed_port(8025)}"


def iter_env() -> Iterator[tuple[str, str]]:
    """The pins currently in effect — handy for debugging a CI run."""
    for key in (
        "DATABASE_URL",
        "DATABASE_DDL_URL",
        "REDIS_URL",
        "STORAGE_ENDPOINT_URL",
        "SMTP_HOST",
        "MAILPIT_URL",
    ):
        if key in os.environ:
            yield key, os.environ[key]
