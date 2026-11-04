import os
from pathlib import Path

# The assistant's whole personal state lives under one directory. This module is
# the only place that spells its name, so every other caller goes through
# get_user_data_dir() and the on-disk location can move exactly once, here.
_DATA_DIRNAME = 'SaturnAI'


def _app_data_root() -> Path:
    return Path(os.getenv('LOCALAPPDATA', os.path.expanduser('~')))


def get_user_data_dir() -> Path:
    d = _app_data_root() / _DATA_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_desktop_output_dir() -> Path:
    d = Path.home() / 'Desktop' / _DATA_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d
