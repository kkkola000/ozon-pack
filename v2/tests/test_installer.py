"""Exercise native installation and failure paths without privileges or server mutations."""

import os
import subprocess
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[1] / "deploy" / "install.sh"
BOOTSTRAP = r"""
source "$1"
v2_checkout="$TEST_ROOT/checkout"
v2_app="$v2_checkout/v2"
v2_units_dir="$TEST_ROOT/units"
v2_runtime_dir="$TEST_ROOT/runtime"
record() { printf '%s\n' "$*" >> "$TEST_ROOT/trace"; }
v2_require_root() { :; }
git() { record git "$@"; mkdir -p "$v2_app"; }
command() {
    if [[ $CASE == missing_python && $1 == -v && $2 == python3* ]]; then return 1; fi
    builtin command "$@"
}
v2_fake_python() {
    local v2_name=$1
    shift
    record "$v2_name" "$@"
    if [[ $1 == -c ]]; then
        case "$CASE:$v2_name" in
            unsupported_python:*|fallback:python3|only_313:python3|only_313:python3.14|only_312:python3|only_312:python3.14|only_312:python3.13)
                return 1 ;;
        esac
    fi
    if [[ $1 == -m ]]; then
        mkdir -p .venv/bin
        cp -p "$TEST_ROOT/python-stub" .venv/bin/python
    fi
}
python3() { v2_fake_python python3 "$@"; }
python3.14() { v2_fake_python python3.14 "$@"; }
python3.13() { v2_fake_python python3.13 "$@"; }
python3.12() { v2_fake_python python3.12 "$@"; }
docker() { record docker "$@"; return 99; }
apt-get() {
    record apt-get "$@"
    if [[ $CASE == packages_error && $1 == install ]]; then echo 'synthetic package error' >&2; return 1; fi
}
pg_lsclusters() {
    record pg_lsclusters "$@"
    [[ $CASE != clusters_error ]] || return 1
    if [[ $CASE == no_cluster ]]; then echo '15 main 5432 online postgres /unused /unused'; return; fi
    if [[ $CASE == multiple_clusters ]]; then echo '17 main 5432 online postgres /unused /unused'; fi
    if [[ $CASE == cluster_down || $CASE == cluster_start_error ]]; then
        echo '18 main 5432 down postgres /unused /unused'
    elif [[ $CASE == multiple_clusters ]]; then
        echo '18 main 5433 online postgres /unused /unused'
    elif [[ $CASE == standby ]]; then
        echo '18 main 5432 online,recovery postgres /unused /unused'
    else
        echo '18 main 5432 online postgres /unused /unused'
    fi
}
pg_ctlcluster() {
    record pg_ctlcluster "$@"
    if [[ $CASE == cluster_start_error ]]; then echo 'synthetic cluster error' >&2; return 1; fi
}
runuser() {
    record runuser "$@"
    if [[ $* == *'SHOW server_version_num'* ]]; then
        [[ $CASE != connection_error ]] || return 1
        if [[ $CASE == old_server ]]; then echo 150000; else echo 180000; fi
    elif [[ $* == *'FROM pg_roles'* ]]; then
        [[ $CASE != db_read_error ]] || return 1
        if [[ $CASE == db_exists ]]; then echo 1; else echo 0; fi
    else
        cat >/dev/null
        [[ $CASE != db_create_error ]] || return 1
    fi
}
ss() {
    record ss "$@"
    [[ $CASE != ports_error ]] || return 1
    if [[ $CASE == ports ]]; then printf '%s\n' 'LISTEN 0 128 127.0.0.1:8001'; fi
}
systemctl() {
    record systemctl "$@"
    if [[ $1 == list-unit-files || $1 == list-units ]]; then
        [[ $CASE != units_error ]] || return 1
        if [[ $CASE == "$1" ]]; then printf '%s\n' 'ozon-pack-v2-web.service enabled'; fi
    fi
}
id() {
    record id "$@"
    if [[ $1 == -gn ]]; then printf '%s\n' ozon-v2; else return 1; fi
}
useradd() { record useradd "$@"; }
chown() { record chown "$@"; }
chmod() { record chmod "$@"; }
curl() {
    record curl "$@"
    [[ $CASE != ready_error ]] || return 22
}
v2_preflight
v2_install
"""
PYTHON_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "python $*" >> "$TEST_ROOT/trace"
if [[ $* == *generate-env* ]]; then
    printf '%s\n' 'synthetic-test-only' > .env.v2
fi
if [[ $CASE == migration_error && $* == *' migrate' ]]; then exit 2; fi
if [[ $* == *native_database.py* ]]; then printf '%s\n' 'SELECT 1;'; fi
"""


def run_installer(tmp_path, case="ok"):
    for directory in ("units", "runtime", "legacy"):
        (tmp_path / directory).mkdir(exist_ok=True)
    (tmp_path / "legacy" / "data.sqlite").write_bytes(b"legacy-data-must-survive")
    stub = tmp_path / "python-stub"
    stub.write_text(PYTHON_STUB)
    stub.chmod(0o700)
    if case == "checkout":
        (tmp_path / "checkout").mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("PACK_V2_")}
    if case == "inherited_config":
        env["PACK_V2_DATABASE_URL"] = "postgresql+psycopg://unused/another_database"
    result = subprocess.run(
        ["bash", "-c", BOOTSTRAP, "installer-test", str(INSTALLER)],
        cwd=tmp_path,
        env={
            **env,
            "TEST_ROOT": str(tmp_path),
            "CASE": case,
            "COMPOSE_FILE": "/not-the-v2/compose.yaml",
            "COMPOSE_PROJECT_NAME": "legacy-panel",
        },
        text=True,
        capture_output=True,
        timeout=15,
    )
    trace = (tmp_path / "trace").read_text() if (tmp_path / "trace").exists() else ""
    assert (tmp_path / "legacy" / "data.sqlite").read_bytes() == b"legacy-data-must-survive"
    return result, trace


def test_installer_shell_syntax():
    subprocess.run(["bash", "-n", str(INSTALLER)], check=True)


def test_sourcing_installer_does_not_execute_installation():
    result = subprocess.run(
        ["bash", "-c", 'source "$1"', "source-test", str(INSTALLER)],
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert not result.stdout and not result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="The caller is already root")
def test_root_is_required_for_installation():
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; v2_require_root', "root-test", str(INSTALLER)],
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "root" in result.stderr


@pytest.mark.parametrize(
    "case",
    [
        "checkout",
        "ports",
        "list-unit-files",
        "list-units",
        "ports_error",
        "units_error",
        "inherited_config",
        "missing_python",
        "unsupported_python",
        "packages_error",
        "clusters_error",
        "no_cluster",
        "old_server",
        "standby",
        "cluster_start_error",
        "connection_error",
        "db_exists",
        "db_read_error",
    ],
)
def test_existing_data_or_unreadable_preflight_stops_before_installation(tmp_path, case):
    result, trace = run_installer(tmp_path, case)
    assert result.returncode != 0, result.stdout
    assert result.stderr
    assert "git clone" not in trace
    assert "docker " not in trace
    assert not list((tmp_path / "units").iterdir())


def test_installation_creates_separate_web_and_worker_services(tmp_path):
    result, trace = run_installer(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "--branch refactor/modular-architecture" in trace
    assert "apt-get install -y --no-install-recommends python3 python3-venv" in trace
    assert "postgresql postgresql-client" in trace
    assert "generate-env --native-postgres --database-port 5432" in trace
    assert "--host=/var/run/postgresql --port=5432 --username=postgres --dbname=postgres" in trace
    assert "native_database.py --env-file .env.v2" in trace
    assert "docker" not in trace
    assert "synthetic-test-only" not in trace + result.stdout + result.stderr
    assert trace.index(" generate-env") < trace.index(" migrate") < trace.index(" create-owner")
    assert trace.index(" migrate") < trace.index("systemctl enable")
    assert "--login admin" in trace
    assert "useradd --system --user-group" in trace
    assert (
        "systemctl enable --now postgresql.service ozon-pack-v2-web.service ozon-pack-v2-worker.service"
        in trace
    )
    assert "/opt/ozon-pack/" not in trace
    env = tmp_path / "checkout" / "v2" / ".env.v2"
    assert env.stat().st_mode & 0o777 == 0o600
    for part in ("web", "worker"):
        unit = (tmp_path / "units" / f"ozon-pack-v2-{part}.service").read_text()
        assert "User=ozon-v2\nGroup=ozon-v2" in unit
        assert f"WorkingDirectory={tmp_path}/checkout/v2" in unit
        assert f"--env-file {tmp_path}/checkout/v2/.env.v2" in unit
        assert "NoNewPrivileges=true" in unit
        assert "WantedBy=multi-user.target" in unit
        assert "After=network-online.target postgresql.service" in unit
        assert "docker" not in unit
        if part == "web":
            assert "--host 127.0.0.1 --port 8001" in unit
        else:
            assert " worker\n" in unit
    assert "v2 установлена" in result.stdout


@pytest.mark.parametrize(
    ("case", "python"),
    [("ok", "python3"), ("fallback", "python3.14"), ("only_313", "python3.13"), ("only_312", "python3.12")],
)
def test_venv_uses_the_first_supported_available_python(tmp_path, case, python):
    result, trace = run_installer(tmp_path, case)
    assert result.returncode == 0, result.stderr
    assert f"{python} -m venv .venv" in trace
    assert trace.count(" -m venv .venv") == 1


def test_failed_migration_never_starts_services(tmp_path):
    result, trace = run_installer(tmp_path, "migration_error")
    assert result.returncode != 0
    assert "create-owner" not in trace
    assert "systemctl enable" not in trace
    assert not list((tmp_path / "units").iterdir())


def test_failed_database_creation_never_runs_migrations_or_starts_app(tmp_path):
    result, trace = run_installer(tmp_path, "db_create_error")
    assert result.returncode != 0
    assert " migrate" not in trace and "systemctl enable" not in trace


def test_native_database_uses_the_selected_cluster_port(tmp_path):
    result, trace = run_installer(tmp_path, "multiple_clusters")
    assert result.returncode == 0, result.stderr
    assert "--port=5433" in trace
    assert "--database-port 5433" in trace


def test_stopped_main_cluster_is_started_without_restarting_others(tmp_path):
    result, trace = run_installer(tmp_path, "cluster_down")
    assert result.returncode == 0, result.stderr
    assert "pg_ctlcluster 18 main start" in trace
    assert "restart" not in trace


def test_failed_readiness_is_not_reported_as_success(tmp_path):
    result, _ = run_installer(tmp_path, "ready_error")
    assert result.returncode != 0
    assert "v2 установлена" not in result.stdout
