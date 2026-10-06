"""Exercise the resolved receiver command without starting containers."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]


@pytest.fixture(scope="module")
def compose():
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose is required for deployment tests")
    version = subprocess.run(
        [docker, "compose", "version"], capture_output=True, text=True, timeout=30,
    )
    if version.returncode:
        pytest.skip("Docker Compose is required for deployment tests")

    def resolve(overrides=None, *, project="receiver-test", development=False):
        # Do not load a developer's .env or inherit receiver/bridge overrides.
        env = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
        env.update(overrides or {})
        args = [
            docker, "compose", "--env-file", str(ROOT / ".env.example"),
            "-p", project, "-f", str(ROOT / "docker-compose.yml"),
        ]
        if development:
            args.extend(["-f", str(ROOT / "docker-compose.dev.yml")])
        result = subprocess.run(
            [*args, "config", "--format", "json"], env=env,
            capture_output=True, text=True, check=True, timeout=30,
        )
        # `compose config` doubles dollars so its output can be reused as input.
        # Restore the actual model before executing the container's shell command.
        return json.loads(result.stdout.replace("$$", "$"))

    return resolve


@pytest.fixture
def launch_receiver(tmp_path):
    # Record the arguments passed to the image's startup script, without Avahi,
    # NQPTP, networking, or Shairport itself. No receiver name is evaluated as code.
    startup = tmp_path / "run.sh"
    startup.write_text("#!/bin/sh\nprintf '%s\\0' \"$@\"\n")
    startup.chmod(0o755)

    def launch(model):
        receiver = model["services"]["shairport-sync"]
        result = subprocess.run(
            [*receiver["entrypoint"], *receiver["command"]],
            env=receiver["environment"], cwd=tmp_path,
            capture_output=True, timeout=10,
        )
        args = result.stdout.decode().rstrip("\0").split("\0") if result.stdout else []
        return result, args

    return launch


@pytest.mark.parametrize("overrides,service_type,name", [
    ({}, "airplay2", "AirPlay 2 Bridge"),
    ({"AIRPLAY_VERSION": "1"}, "classic", "AirPlay 1 Bridge"),
    ({"AIRPLAY_VERSION": "2"}, "airplay2", "AirPlay 2 Bridge"),
    ({"AIRPLAY_VERSION": "", "AIRPLAY_NAME": ""}, "airplay2", "AirPlay 2 Bridge"),
])
def test_receiver_modes_use_upstream_startup_with_only_mode_and_name(
    compose, launch_receiver, overrides, service_type, name,
):
    result, args = launch_receiver(compose(overrides))
    assert result.returncode == 0, result.stderr.decode()
    assert args == [f"--service-type={service_type}", "-a", name]


@pytest.mark.parametrize("version", ["1", "2"])
@pytest.mark.parametrize("name", [
    "Living Room",
    'Kitchen & Dining: "Speaker"',
    "Speaker's $name $$ ${HOME} $(exit 23); `exit 24`",
])
def test_receiver_name_is_one_literal_argument(compose, launch_receiver, version, name):
    result, args = launch_receiver(compose({"AIRPLAY_VERSION": version, "AIRPLAY_NAME": name}))
    assert result.returncode == 0, result.stderr.decode()
    assert args[-2:] == ["-a", name]
    assert len(args) == 3


@pytest.mark.parametrize("version", ["0", "3", "classic", "1; exit 0"])
def test_invalid_receiver_mode_fails_before_upstream_startup(compose, launch_receiver, version):
    result, args = launch_receiver(compose({"AIRPLAY_VERSION": version}))
    assert result.returncode != 0
    assert "set AIRPLAY_VERSION to 1 or 2 in .env" in result.stderr.decode()
    assert args == []


def test_separate_endpoints_have_independent_stream_ports_and_volumes(compose):
    classic = compose({"AIRPLAY_VERSION": "1", "STREAM_PORT": "8001"}, project="classic-test")
    airplay2 = compose(project="airplay2-test")
    for model, port in [(classic, "8001"), (airplay2, "8000")]:
        bridge = model["services"]["bridge"]
        assert bridge["ports"][0]["published"] == port
        assert bridge["environment"]["STREAM_URL"] == f"http://192.0.2.10:{port}/stream.flac"
        assert all("container_name" not in service for service in model["services"].values())
    assert classic["volumes"]["shared"]["name"] != airplay2["volumes"]["shared"]["name"]


def test_development_override_preserves_receiver_launch(compose, launch_receiver):
    model = compose({"AIRPLAY_VERSION": "1", "AIRPLAY_NAME": "Development"}, development=True)
    result, args = launch_receiver(model)
    assert result.returncode == 0, result.stderr.decode()
    assert args == ["--service-type=classic", "-a", "Development"]
    assert model["services"]["bridge"]["build"]["context"] == str(ROOT / "bridge")
