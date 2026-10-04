import importlib.util
from pathlib import Path

import pytest

from audio_helpers import AudioPipe


@pytest.fixture
def bridge():
    """Load fresh module state without opening pipes or starting the server."""
    path = Path(__file__).parents[1] / "bridge" / "server.py"
    spec = importlib.util.spec_from_file_location("bridge_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def audio_pipe():
    """Create pipes and clean up even when a race assertion fails."""
    pipes = []

    def create(audio=None):
        pipe = AudioPipe(audio)
        pipes.append(pipe)
        return pipe

    yield create
    for pipe in reversed(pipes):
        pipe.close()
