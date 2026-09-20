#!/usr/bin/env python3
"""Deploy an immutable portfolio image on the Raspberry Pi with Docker Compose.

Runs on the Pi, standard library only, uploaded fresh for every deployment so the host
keeps no copy of the tooling. State lives under ``<root>/``::

    deploy.lock              host-wide lock, so two deployments can never interleave
    prod/secrets.env         operator-managed secrets (chmod 600, never read by this script)
    prod/current.json        the last healthy deployment, written atomically
    prod/attempts/<id>/      compose.yml, request.json, previous.json, the database backup
                             and result.json for each attempt

Safety properties, in the order they are enforced:

* every argument is validated before any command runs;
* the image must be a digest belonging to this repository, never a mutable tag;
* the image's OCI labels must match the requested revision and version, so a digest that
  does not correspond to the commit CI claims is refused;
* an older workflow run can never replace a newer deployment (rerun protection);
* the live SQLite database is backed up, with an integrity check, before anything is
  replaced;
* the new container must report healthy *and* be running the exact digest, otherwise the
  previous deployment is restored.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPOSITORY = "emanuelturtula/portfolio"
IMAGE = re.compile(rf"ghcr\.io/{re.escape(REPOSITORY)}@sha256:[0-9a-f]{{64}}")
REVISION = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"v\d+\.\d+\.\d+")
RUN_URL = re.compile(rf"https://github\.com/{re.escape(REPOSITORY)}/actions/runs/[1-9][0-9]*")
ENVIRONMENTS = {"prod": "8083"}
PROJECT_PREFIX = "portfolio-app"
DATABASE = "/app/data/portfolio.db"
WAIT_TIMEOUT_SECONDS = "180"
KEEP_ATTEMPTS = 10

Manifest = dict[str, Any]


class DeploymentError(RuntimeError):
    pass


def run(args: list[str], *, env: dict[str, str] | None = None) -> str:
    try:
        return subprocess.run(
            args, env=env, check=True, text=True, capture_output=True, timeout=900
        ).stdout.strip()
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "No diagnostic output").strip()
        detail = "".join(c for c in detail if c.isprintable() or c == "\n")
        raise DeploymentError(f"Command failed ({error.returncode}): {detail[-4000:]}") from error


def read_json(path: Path) -> Manifest:
    data: Manifest = json.loads(path.read_text(encoding="utf-8"))
    return data


def write_json(path: Path, value: Manifest, *, immutable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if immutable:
        with path.open("x", encoding="utf-8") as target:
            target.write(data)
        return
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(data, encoding="utf-8")
    temporary.replace(path)


@contextmanager
def deployment_lock(root: Path, timeout_seconds: float = 240) -> Iterator[None]:
    import fcntl  # Linux only; imported lazily so the unit tests run on any platform.

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    previous_umask = os.umask(0o077)
    try:
        with (root / "deploy.lock").open("a") as lock:
            deadline = time.monotonic() + timeout_seconds
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise DeploymentError(
                            "The deployment host is busy; retry once the active "
                            "deployment finishes"
                        ) from None
                    time.sleep(0.25)
            yield
    finally:
        os.umask(previous_umask)


def validate(
    environment: str,
    image: str,
    revision: str,
    version: str,
    run_number: int,
    source_run_url: str,
) -> None:
    if environment not in ENVIRONMENTS:
        raise DeploymentError(f"Environment must be one of {sorted(ENVIRONMENTS)}")
    if not IMAGE.fullmatch(image):
        raise DeploymentError("The image must be an immutable digest belonging to this repository")
    if not REVISION.fullmatch(revision):
        raise DeploymentError("A full 40-character commit SHA is required")
    if not VERSION.fullmatch(version):
        raise DeploymentError(f"Version {version!r} is not a valid release version")
    if type(run_number) is not int or run_number < 1:
        raise DeploymentError("A positive workflow run number is required")
    if not RUN_URL.fullmatch(source_run_url):
        raise DeploymentError("A workflow run URL for this repository is required")


def prepare_secrets_env_file(root: Path, environment: str) -> Path:
    """Guarantee ``<root>/<env>/secrets.env`` exists and is private.

    The operator writes this file over SSH. This script never reads, writes or logs its
    contents; it only creates an empty file on first install and refuses to continue if
    the permissions have been loosened.
    """
    path = root / environment / "secrets.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
        os.chmod(path, 0o600)
    elif path.stat().st_mode & 0o077:
        raise DeploymentError(
            f"{path} must not be group or world readable; run 'chmod 600' on it and retry"
        )
    return path


def compose(manifest: Manifest, *args: str) -> str:
    environment = os.environ.copy()
    environment.update(
        PORTFOLIO_IMAGE=manifest["image"],
        PORTFOLIO_PORT=ENVIRONMENTS[manifest["environment"]],
        PORTFOLIO_ENVIRONMENT=manifest["environment"],
        PORTFOLIO_SECRETS_ENV_FILE=manifest.get("secrets_env_file", ""),
    )
    return run(
        [
            "docker",
            "compose",
            "--project-name",
            f"{PROJECT_PREFIX}-{manifest['environment']}",
            "--file",
            manifest["compose"],
            *args,
        ],
        env=environment,
    )


def verify_running(manifest: Manifest) -> str:
    container = compose(manifest, "ps", "--quiet", "app")
    if not container or "\n" in container:
        raise DeploymentError("Expected exactly one running application container")
    info = json.loads(run(["docker", "inspect", container]))[0]
    if info["Config"]["Image"] != manifest["image"]:
        raise DeploymentError("The running container does not match the requested image")
    if info["State"].get("Health", {}).get("Status") != "healthy":
        raise DeploymentError("The application health check did not pass")
    return str(container)


def backup(previous: Manifest, attempt: Path) -> str | None:
    """Copy the live SQLite database into the attempt directory, if one exists.

    Uses sqlite3's own backup API inside the running container rather than copying the
    file, so the snapshot is consistent even while the application is writing, and
    verifies it with an integrity check before accepting it.
    """
    try:
        container = verify_running(previous)
    except DeploymentError:
        return None  # Nothing healthy to back up; this deployment may well be the fix.
    inner_path = f"/app/data/deploy-backup-{attempt.name}.sqlite3"
    script = (
        "import os,sqlite3,sys\n"
        f"if not os.path.exists({DATABASE!r}): print('absent'); sys.exit(0)\n"
        f"source=sqlite3.connect('file:{DATABASE}?mode=ro',uri=True)\n"
        "target=sqlite3.connect(sys.argv[1]); source.backup(target)\n"
        "assert target.execute('PRAGMA integrity_check').fetchone()[0]=='ok'\n"
        "target.close(); source.close(); print('ok')"
    )
    if run(["docker", "exec", container, "python", "-c", script, inner_path]) == "absent":
        return None
    destination = attempt / "database.sqlite3"
    run(["docker", "cp", f"{container}:{inner_path}", str(destination)])
    run(["docker", "exec", container, "rm", "-f", inner_path])
    return str(destination)


def check_run_order(
    previous: Manifest | None, image: str, revision: str, run_number: int
) -> None:
    """Refuse a workflow run older than the one already deployed.

    Re-running an old workflow from the Actions UI would otherwise silently roll
    production back to a previous commit.
    """
    if not previous or "delivery" not in previous:
        return
    last = previous["delivery"]
    same_artifact = (image, revision) == (previous["image"], previous["revision"])
    if run_number < last["run_number"] or (
        run_number == last["run_number"] and not same_artifact
    ):
        raise DeploymentError(
            "This workflow run is older than, or conflicts with, the deployed one; "
            "push a new commit instead of re-running an old workflow"
        )


def prune_attempts(attempts: Path, protected: set[str]) -> None:
    existing = sorted(path for path in attempts.iterdir() if path.is_dir())
    for path in existing[:-KEEP_ATTEMPTS]:
        if str(path) not in protected:
            shutil.rmtree(path, ignore_errors=True)


def deploy(
    environment: str,
    image: str,
    revision: str,
    version: str,
    run_number: int,
    source_run_url: str,
    root: Path,
) -> Manifest:
    validate(environment, image, revision, version, run_number, source_run_url)
    root = Path(root).expanduser().resolve()
    with deployment_lock(root):
        return deploy_locked(
            environment, image, revision, version, run_number, source_run_url, root
        )


def deploy_locked(
    environment: str,
    image: str,
    revision: str,
    version: str,
    run_number: int,
    source_run_url: str,
    root: Path,
) -> Manifest:
    current_path = root / environment / "current.json"
    previous = read_json(current_path) if current_path.exists() else None
    check_run_order(previous, image, revision, run_number)

    # Pull and check provenance before touching the running service.
    run(["docker", "pull", image])
    labels = json.loads(run(["docker", "image", "inspect", image]))[0]["Config"].get("Labels") or {}
    if labels.get("org.opencontainers.image.revision") != revision:
        raise DeploymentError("The OCI revision label does not match the requested commit")
    if labels.get("org.opencontainers.image.version") != version:
        raise DeploymentError("The OCI version label does not match the requested version")

    attempts = root / environment / "attempts"
    attempt = attempts / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}"
    attempt.mkdir(parents=True)
    compose_path = attempt / "compose.yml"
    shutil.copyfile(Path(__file__).with_name("compose.yml"), compose_path)
    candidate: Manifest = {
        "environment": environment,
        "image": image,
        "revision": revision,
        "version": version,
        "compose": str(compose_path),
        "secrets_env_file": str(prepare_secrets_env_file(root, environment)),
        "status": "pending",
        "attempt": str(attempt),
        "delivery": {"run_number": run_number, "source_run_url": source_run_url},
    }
    write_json(attempt / "request.json", candidate, immutable=True)

    if previous:
        write_json(attempt / "previous.json", previous, immutable=True)
        try:
            candidate["backup"] = backup(previous, attempt)
        except Exception as failure:
            write_json(
                attempt / "result.json",
                dict(candidate, status="failed", stage="backup", error=str(failure)),
                immutable=True,
            )
            raise DeploymentError(
                f"The backup failed before the service was replaced: {failure}"
            ) from failure

    try:
        compose(
            candidate, "up", "--detach", "--wait", "--wait-timeout", WAIT_TIMEOUT_SECONDS, "app"
        )
        verify_running(candidate)
    except Exception as failure:
        result = dict(candidate, status="failed", error=str(failure))
        try:
            if previous:
                compose(
                    previous,
                    "up",
                    "--detach",
                    "--wait",
                    "--wait-timeout",
                    WAIT_TIMEOUT_SECONDS,
                    "app",
                )
                verify_running(previous)
                result["rollback"] = "healthy"
            else:
                compose(candidate, "down")  # Keeps the data volume.
                result["rollback"] = "no_previous_deployment"
        except Exception as rollback_failure:
            result["rollback"] = "failed"
            result["rollback_error"] = str(rollback_failure)
        write_json(attempt / "result.json", result, immutable=True)
        raise DeploymentError(
            f"Deployment failed; rollback={result['rollback']}; evidence={attempt}"
        ) from failure

    candidate.update(status="healthy", deployed_at=datetime.now(UTC).isoformat())
    write_json(attempt / "result.json", candidate, immutable=True)
    write_json(current_path, candidate)
    protected = {str(attempt), str(previous["attempt"]) if previous else ""}
    prune_attempts(attempts, protected)
    return candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=tuple(ENVIRONMENTS), required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--run-number", type=int, required=True)
    parser.add_argument("--source-run-url", required=True)
    parser.add_argument("--root", type=Path, default=Path.home() / "portfolio-app-deploy")
    args = parser.parse_args(argv)
    try:
        result = deploy(**vars(args))
    except (DeploymentError, subprocess.SubprocessError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
