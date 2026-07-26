"""PlayCall NDI Encoder — Pi-based RTMP relay + scorebug overlay."""

from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent.parent / 'VERSION'
try:
    __version__ = _VERSION_FILE.read_text().strip()
except OSError:
    __version__ = '0.0.0'
