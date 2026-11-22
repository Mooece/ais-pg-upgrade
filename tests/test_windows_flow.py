"""Run the real PowerShell flow against controlled command and TCP fixtures."""

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell integration")
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def flow(tmp_path):
    project = tmp_path / "original 中文 & space"
    addon = tmp_path / "addon 中文 & space"
    project.mkdir()
    addon.mkdir()
    source_venv = Path(sys.executable).resolve().parent.parent
    # The command adapter uses a real interpreter, but never imports the application.
    venv = project / ".venv"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(venv), str(source_venv)],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    (project / "main.py").write_text("installed fixture\n", encoding="utf-8")
    shutil.copyfile(ROOT / "Upgrade-Windows.ps1", addon / "Upgrade-Windows.ps1")
    shutil.copyfile(ROOT / "Upgrade-Windows.cmd", addon / "Upgrade-Windows.cmd")
    (addon / "invoke-wrapper.ps1").write_text(
        "param([string]$Wrapper, [string]$Project, [int]$Port)\n"
        "& $Wrapper $Project $Port\nexit $LASTEXITCODE\n",
        encoding="utf-8",
    )
    (addon / "upgrade.py").write_text(
        "import json, os, pathlib, sys\n"
        'log = pathlib.Path(os.environ["FLOW_LOG"])\n'
        'with log.open("a", encoding="utf-8") as out: out.write(json.dumps(sys.argv[1:]) + "\\n")\n'
        'sys.exit(7 if sys.argv[1] == os.environ.get("FLOW_FAIL") else 0)\n',
        encoding="utf-8",
    )
    (project / "start.bat").write_text(
        '@echo off\n"%~dp0.venv\\Scripts\\python.exe" -B "%~dp0start-probe.py" %*\n'
        "exit /b %FLOW_START_CODE%\n",
        encoding="ascii",
    )
    (project / "start-probe.py").write_text(
        "import json, os, pathlib, sys\n"
        'pathlib.Path(os.environ["FLOW_START"]).write_text(json.dumps({\n'
        '    "cwd": os.getcwd(), "port": os.environ.get("AISTUDIO_PORT"),\n'
        '    "args": sys.argv[1:]}), encoding="utf-8")\n',
        encoding="utf-8",
    )
    env = dict(
        os.environ,
        FLOW_LOG=str(tmp_path / "commands.jsonl"),
        FLOW_START=str(tmp_path / "start.txt"),
        FLOW_START_CODE="0",
        PYTHONDONTWRITEBYTECODE="1",
    )
    return project, addon, env


def run_flow(flow, port, *, no_start=False):
    project, addon, env = flow
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(addon / "Upgrade-Windows.ps1"),
        "-ProjectPath",
        str(project),
        "-Port",
        str(port),
    ]
    if no_start:
        command.append("-NoStart")
    return subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def commands(flow):
    path = Path(flow[2]["FLOW_LOG"])
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_live_service_is_rejected_before_check_or_apply(flow):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        result = run_flow(flow, listener.getsockname()[1])
    assert result.returncode != 0
    assert "is in use" in result.stdout + result.stderr
    assert not commands(flow)
    assert not Path(flow[2]["FLOW_START"]).exists()


def test_success_checks_applies_then_starts_in_original_directory(flow):
    port = free_port()
    result = run_flow(flow, port)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [row[0] for row in commands(flow)] == ["check", "apply"]
    assert all(row[2] == str(flow[0]) for row in commands(flow))
    started = json.loads(Path(flow[2]["FLOW_START"]).read_text(encoding="utf-8"))
    assert started == {"cwd": str(flow[0]), "port": str(port), "args": ["--port", str(port)]}


def test_no_start_mode_applies_without_launching_a_service(flow):
    result = run_flow(flow, free_port(), no_start=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [row[0] for row in commands(flow)] == ["check", "apply"]
    assert not Path(flow[2]["FLOW_START"]).exists()


@pytest.mark.parametrize("stage,expected", [("check", ["check"]), ("apply", ["check", "apply"])])
def test_failure_stops_before_service_start(flow, stage, expected):
    flow[2]["FLOW_FAIL"] = stage
    result = run_flow(flow, free_port())
    assert result.returncode != 0
    assert [row[0] for row in commands(flow)] == expected
    assert not Path(flow[2]["FLOW_START"]).exists()


def test_missing_original_start_entry_is_rejected_before_apply(flow):
    (flow[0] / "start.bat").unlink()
    result = run_flow(flow, free_port())
    assert result.returncode != 0
    assert "Required file is missing" in result.stdout + result.stderr
    assert not commands(flow)


def test_original_start_failure_is_reported(flow):
    flow[2]["FLOW_START_CODE"] = "12"
    result = run_flow(flow, free_port())
    assert result.returncode != 0
    assert "exited with code 12" in result.stdout + result.stderr


def run_cmd_wrapper(flow, port):
    project, addon, env = flow
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(addon / "invoke-wrapper.ps1"),
            "-Wrapper",
            str(addon / "Upgrade-Windows.cmd"),
            "-Project",
            str(project),
            "-Port",
            str(port),
        ],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=30,
    )


def test_cmd_wrapper_rejects_live_service_before_apply(flow):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        result = run_cmd_wrapper(flow, listener.getsockname()[1])
    assert result.returncode != 0
    assert "is in use" in result.stdout + result.stderr
    assert not commands(flow)


def test_cmd_wrapper_runs_checked_apply_without_start(flow):
    result = run_cmd_wrapper(flow, free_port())
    assert result.returncode == 0, result.stdout + result.stderr
    assert [row[0] for row in commands(flow)] == ["check", "apply"]
    assert not Path(flow[2]["FLOW_START"]).exists()
