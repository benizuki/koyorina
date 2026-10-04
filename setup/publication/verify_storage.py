"""Compatibility entrypoint for the shared CSI storage check."""

from pathlib import Path
from runpy import run_path


run_path(str(Path(__file__).resolve().parents[1] / 'storage' / 'verify_storage.py'), run_name='__main__')
