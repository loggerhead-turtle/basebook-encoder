import sys
from pathlib import Path

import pytest

# Make `import encoder` work no matter where pytest is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Every test gets an isolated config/state dir and full fake mode —
    no /etc, no subprocesses, no network."""
    monkeypatch.setenv('PLAYCALL_ENCODER_DIR', str(tmp_path / 'config'))
    monkeypatch.setenv('PLAYCALL_ENCODER_STATE', str(tmp_path / 'state'))
    monkeypatch.setenv('SCOREBUG_FAKE', '1')
    return tmp_path
