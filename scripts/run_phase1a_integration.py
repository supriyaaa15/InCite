"""Run Phase 1A tests from scratch storage with a fresh local PostgreSQL 16."""

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4


REPO = Path(__file__).resolve().parents[1]
IMAGE = "postgres:16"
USER = "incite_test"
LABEL = "incite.phase1a.run"
PGDATA = "/var/lib/postgresql/data"


def scratch_env(scratch):
    env = {name: os.environ[name] for name in ("SystemRoot", "WINDIR") if name in os.environ}
    env.update(
        PATH=str(Path(env["SystemRoot"]) / "System32") if os.name == "nt" else os.defpath,
        HOME=str(scratch),
        USERPROFILE=str(scratch),
        TEMP=str(scratch),
        TMP=str(scratch),
        PYTHONPATH=str(REPO),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        JWT_SECRET=secrets.token_hex(32),
        GROQ_API_KEY="synthetic-phase1a-key",
        DATABASE_URL="sqlite://",
        CHROMA_PERSIST_PATH=str(scratch / "chroma"),
        UPLOAD_DIR=str(scratch / "uploads"),
        HF_HOME=str(scratch / "huggingface"),
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        ANONYMIZED_TELEMETRY="FALSE",
        OTEL_SDK_DISABLED="true",
    )
    return env


def pytest_run(scratch, env, *targets):
    config = scratch / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    command = [
        sys.executable, "-B", "-s", "-m", "pytest", "-c", str(config),
        "--rootdir", str(REPO), "-p", "no:cacheprovider", "-ra", *map(str, targets),
    ]
    print(subprocess.list2cmdline(command), flush=True)
    return subprocess.run(command, cwd=scratch, env=env, check=False).returncode


def integration_run(scratch, env, *, pull):
    docker_exe = shutil.which("docker")
    if not docker_exe:
        raise RuntimeError("Docker CLI is missing from PATH.")
    endpoint = "npipe:////./pipe/dockerDesktopLinuxEngine" if os.name == "nt" else "unix:///var/run/docker.sock"
    docker_config = scratch / "docker"
    docker_config.mkdir()
    run_id = uuid4().hex
    name = f"incite-phase1a-{run_id}"
    database = f"incite_phase1a_{run_id}"
    password = secrets.token_hex(24)
    docker_env = {key: env[key] for key in ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP") if key in env}
    docker_env["POSTGRES_PASSWORD"] = password

    def docker(*args, check=True):
        return subprocess.run(
            [docker_exe, "--config", str(docker_config), "--host", endpoint, *args],
            cwd=scratch, env=docker_env, capture_output=True, text=True, check=check, timeout=300,
        )

    engine_os = docker("version", "--format", "{{.Server.Os}}").stdout.strip()
    if engine_os != "linux":
        raise RuntimeError(f"Expected a local Linux Docker engine, got {engine_os!r}.")
    if pull:
        docker("pull", IMAGE)
    image = docker("image", "inspect", "--format", "{{.Id}}", IMAGE, check=False)
    if image.returncode:
        raise RuntimeError(f"{IMAGE} is missing locally. Use --pull to fetch the official image.")
    image_id = image.stdout.strip()
    cid_file = scratch / "postgres.cid"

    def inspect_owned(container_id):
        result = docker("container", "inspect", container_id)
        info, = json.loads(result.stdout)
        if (
            info["Id"] != container_id or info["Name"] != f"/{name}"
            or info["Config"]["Labels"].get(LABEL) != run_id
        ):
            raise RuntimeError(f"Container identity could not be verified for {name}. Inspect it manually.")
        return info

    try:
        docker(
            "run", "--detach", "--rm", "--pull=never", "--name", name,
            "--cidfile", str(cid_file), "--label", f"{LABEL}={run_id}",
            "--tmpfs", PGDATA, "--publish", "127.0.0.1::5432",
            "--env", f"POSTGRES_USER={USER}", "--env", f"POSTGRES_DB={database}",
            "--env", "POSTGRES_PASSWORD", image_id,
        )
        container_id = cid_file.read_text(encoding="utf-8").strip()
        info = inspect_owned(container_id)
        bindings = info["NetworkSettings"]["Ports"].get("5432/tcp")
        if not bindings or len(bindings) != 1 or bindings[0]["HostIp"] != "127.0.0.1":
            raise RuntimeError("PostgreSQL must publish exactly one loopback-only port.")
        port = int(bindings[0]["HostPort"])
        if (
            info["Image"] != image_id or not info["HostConfig"]["AutoRemove"]
            or PGDATA not in (info["HostConfig"]["Tmpfs"] or {}) or info["HostConfig"]["Binds"]
            or any(mount["Type"] != "tmpfs" for mount in info["Mounts"])
            or not 1024 < port < 65536 or port == 5432
        ):
            raise RuntimeError("Unexpected image, storage or port on the disposable container.")
        print(f"Disposable container {name} ({container_id[:12]}) at 127.0.0.1:{port}, image {image_id}", flush=True)

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            # The temporary initialization server has no TCP listener.
            ready = docker(
                "exec", container_id, "pg_isready", "-h", "127.0.0.1", "-U", USER, "-d", database,
                check=False,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Disposable PostgreSQL did not become ready in 60 seconds.")

        identity = docker(
            "exec", container_id, "psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-U", USER, "-d", database,
            "-c", "SELECT current_database(), current_setting('server_version_num'), system_identifier, "
            "current_setting('server_version') FROM pg_control_system()",
        ).stdout.strip()
        actual_db, version, system_id, server_version = identity.split("|")
        if actual_db != database or not 160000 <= int(version) < 170000 or not system_id.isdecimal():
            raise RuntimeError("The container did not report the expected fresh PostgreSQL 16 database.")
        print(f"Container reports PostgreSQL {server_version} in {actual_db}", flush=True)
        env.update(
            INCITE_PHASE1A_RUN=run_id,
            INCITE_PHASE1A_SYSTEM_ID=system_id,
            DATABASE_URL=f"postgresql+psycopg2://{USER}:{password}@127.0.0.1:{port}/{database}?sslmode=disable&connect_timeout=5",
        )
        return pytest_run(scratch, env, "-v", REPO / "tests" / "integration" / "test_phase1a.py")
    finally:
        if cid_file.is_file():
            # Docker handles removal with --rm. Never stop a different container.
            container_id = cid_file.read_text(encoding="utf-8").strip()
            inspect_owned(container_id)
            docker("stop", container_id)
            remaining = docker("ps", "-a", "--filter", f"id={container_id}", "--format", "{{.ID}}")
            if remaining.stdout.strip():
                raise RuntimeError(f"Disposable container {name} did not auto-remove.")
            print(f"Removed disposable container {name}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--pull", action="store_true", help="pull the official postgres:16 image before running")
    mode.add_argument("--unit", action="store_true", help="run local unit tests without Docker")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 11) or sys.prefix == sys.base_prefix:
        parser.error("Run with the project's Python 3.11 virtual environment.")
    try:
        with TemporaryDirectory(prefix="incite-phase1a-") as path:
            scratch = Path(path).resolve()
            env = scratch_env(scratch)
            if args.unit:
                return pytest_run(scratch, env, "--ignore", REPO / "tests" / "integration", REPO / "tests")
            return integration_run(scratch, env, pull=args.pull)
    except subprocess.CalledProcessError as exc:
        # The command can contain test configuration. Report only Docker's diagnostic.
        print(f"Docker command failed (exit {exc.returncode}): {exc.stderr.strip()}", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"Phase 1A runner: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
