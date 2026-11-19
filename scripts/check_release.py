"""Audit the explicit source set and, optionally, built distribution archives.

This command reads files only. Local test collections, model weights, settings,
databases and development/limitation documents are never release inputs.
"""
from pathlib import Path, PurePosixPath
import argparse
import re
import subprocess
import tarfile
import zipfile


ROOT_FILES = {
    '.gitignore', '.gitattributes', 'pyproject.toml', 'MANIFEST.in', 'README.md',
    'requirements.txt', 'run.py', 'setup.bat', 'start.bat', 'fix_cuda.bat', 'install_runtime.py',
}
PATTERNS = {
    'photocleaner': '*.py', 'help': '*.md', 'defaults': '*.json',
    'tests/unit': '*.py', 'scripts': '*.py', '.github/workflows': '*.yml',
}
EGG_FILES = {'PKG-INFO', 'SOURCES.txt', 'dependency_links.txt', 'entry_points.txt', 'requires.txt', 'top_level.txt'}


def source_paths(root):
    paths = {root / name for name in ROOT_FILES if (root / name).is_file()}
    for directory, pattern in PATTERNS.items():
        paths.update(path for path in (root / directory).rglob(pattern) if '__pycache__' not in path.parts)
    return sorted(paths)


def allowed_source(relative):
    name = PurePosixPath(relative)
    if name.is_absolute() or '..' in name.parts:
        return False
    if str(name) in ROOT_FILES:
        return True
    for directory, pattern in PATTERNS.items():
        base = PurePosixPath(directory)
        if name.is_relative_to(base) and name.match(pattern) and '__pycache__' not in name.parts:
            return True
    return False


def audit_archive(path):
    if path.suffix == '.whl':
        with zipfile.ZipFile(path) as archive:
            names = [item.filename for item in archive.infolist() if not item.is_dir()]
        mapped = []
        for name in names:
            parts = PurePosixPath(name).parts
            if any(p.endswith('.dist-info') for p in parts):
                continue
            marker = 'share/photocleaner/'
            mapped.append(name.split(marker, 1)[1] if marker in name else name)
    else:
        with tarfile.open(path) as archive:
            names = [item.name for item in archive.getmembers() if item.isfile()]
        mapped = []
        for name in names:
            relative = '/'.join(PurePosixPath(name).parts[1:])
            parts = PurePosixPath(relative).parts
            if relative in ('PKG-INFO', 'setup.cfg') or (parts and parts[0].endswith('.egg-info') and parts[-1] in EGG_FILES):
                continue
            mapped.append(relative)
    invalid = [name for name in mapped if not allowed_source(name)]
    if invalid:
        raise ValueError('Unexpected archive members: ' + ', '.join(invalid[:20]))
    if not any(name.startswith('help/') for name in mapped) or not any(name.startswith('defaults/') for name in mapped):
        raise ValueError('Runtime help or default resources are missing')
    return len(names)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, action='append', default=[])
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    paths = source_paths(root)
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if not allowed_source(relative) or path.is_symlink() or path.resolve() != path.absolute():
            raise ValueError('Unexpected source path: ' + relative)
        # Catch machine-specific paths accidentally pasted into maintained code.
        text = path.read_text(encoding='utf-8-sig')
        if re.search(r'[A-Z]:[/\\]Users[/\\][\w .-]+[/\\]',text,re.IGNORECASE):
            raise ValueError('Private workstation path in ' + relative)
    if (root/'.git').is_dir():
        # Use a per-command ownership exception for shared workspaces; never
        # change the user's global Git configuration or stage/commit files.
        git=subprocess.run(['git','-c','safe.directory='+root.as_posix(),'ls-files','--cached','--others','--exclude-standard','-z'],cwd=root,capture_output=True,check=True)
        visible={name for name in git.stdout.decode('utf8').split('\0') if name}
        invalid=sorted(name for name in visible if not allowed_source(name))
        if invalid:raise ValueError('Private/unexpected files visible to Git: '+', '.join(invalid[:20]))
        missing=sorted(path.relative_to(root).as_posix() for path in paths if path.relative_to(root).as_posix() not in visible)
        if missing:raise ValueError('Release sources accidentally ignored by Git: '+', '.join(missing[:20]))
    print(f'Release source audit passed: {len(paths)} files; local collections and private documents excluded.')
    for archive in args.archive:
        print(f'{archive.name}: {audit_archive(archive)} archive members checked.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
