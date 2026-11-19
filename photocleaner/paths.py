"""Separate installed code/resources from writable application state.

A source checkout keeps the existing portable layout. A wheel installation
uses the user's local application directory, so site-packages stays read-only.
PHOTOCLEANER_HOME can explicitly select another writable state directory.
"""
from pathlib import Path
import os
import sys
import sysconfig


SOURCE_ROOT = Path(__file__).resolve().parent.parent


def application_home(source_root=SOURCE_ROOT, environ=None):
    env = os.environ if environ is None else environ
    override = env.get('PHOTOCLEANER_HOME')
    if override:
        return Path(override).expanduser().absolute()
    if (source_root / 'run.py').is_file() and (source_root / 'defaults').is_dir():
        return source_root
    if os.name == 'nt':
        return Path(env.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local') / 'PhotoCleaner'
    return Path(env.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share') / 'photocleaner'


APP_HOME = application_home()


def resource_directory(name):
    if name not in ('help', 'defaults'):
        raise ValueError('Unknown application resource directory')
    candidates = (SOURCE_ROOT / name,
                  Path(sysconfig.get_path('data')) / 'share' / 'photocleaner' / name,
                  Path(sys.prefix) / 'share' / 'photocleaner' / name,
                  SOURCE_ROOT / 'share' / 'photocleaner' / name)
    return next((path for path in candidates if path.is_dir()), candidates[0])


def resource_file(name, filename):
    local = APP_HOME / name / filename
    return local if local.is_file() else resource_directory(name) / filename


def prepare_editable_resources(home=None):
    """Seed editable help/defaults once; preserve every existing local edit."""
    home = Path(home) if home is not None else APP_HOME
    home.mkdir(parents=True, exist_ok=True)
    for name, pattern in (('help', '*.md'), ('defaults', '*.json')):
        source = resource_directory(name)
        destination = home / name
        destination.mkdir(exist_ok=True)
        for item in source.glob(pattern):
            target = destination / item.name
            if item.resolve() == target.resolve():
                continue
            try:
                with target.open('xb') as handle:
                    handle.write(item.read_bytes())
            except FileExistsError:
                pass
