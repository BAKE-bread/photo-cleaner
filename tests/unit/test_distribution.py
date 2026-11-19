from pathlib import Path
from unittest.mock import patch
import importlib.util

from photocleaner import paths


def release_module():
    file = Path(__file__).resolve().parents[2] / 'scripts' / 'check_release.py'
    spec = importlib.util.spec_from_file_location('release_check', file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_set_excludes_private_data_and_documents():
    module = release_module()
    for path in ('settings.json', '.jobs/state.sqlite3', 'models/model.safetensors', 'positive data/a.jpg', 'test-output/report.json', '更新与验证.md', 'defaults/README.md', 'tests/audit_samples.py'):
        assert not module.allowed_source(path), path
    for path in ('photocleaner/engine.py', 'defaults/photo.json', 'help/photo.md', 'tests/unit/test_planning.py', '.github/workflows/checks.yml'):
        assert module.allowed_source(path), path


def test_user_home_preserves_portable_checkouts_and_explicit_override(tmp_path):
    (tmp_path / 'run.py').write_text('',encoding='utf8')
    (tmp_path / 'defaults').mkdir()
    assert paths.application_home(tmp_path, {}) == tmp_path
    override = tmp_path / 'separate-state'
    assert paths.application_home(tmp_path, {'PHOTOCLEANER_HOME':str(override)}) == override


def test_editable_resources_preserve_user_changes(tmp_path):
    bundled = tmp_path / 'bundled'
    for directory in ('help','defaults'):
        (bundled / directory).mkdir(parents=True)
    (bundled / 'help' / 'photo.md').write_text('packaged help',encoding='utf8')
    (bundled / 'defaults' / 'photo.json').write_text('{"min_side":400}',encoding='utf8')
    home = tmp_path / 'home'
    with patch.object(paths, 'resource_directory', lambda name:bundled / name):
        paths.prepare_editable_resources(home)
        (home / 'help' / 'photo.md').write_text('my edited help',encoding='utf8')
        paths.prepare_editable_resources(home)
    assert (home / 'help' / 'photo.md').read_text(encoding='utf8') == 'my edited help'
    assert (home / 'defaults' / 'photo.json').is_file()
