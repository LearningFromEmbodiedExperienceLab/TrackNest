"""Offline personal-project operations; Trackio runs in a fresh helper process."""
import argparse
import os
from pathlib import Path
import shutil
import sys


def project_names(root):
    if root.is_symlink():
        raise ValueError('The personal data directory must not be a symbolic link.')
    return sorted(
        path.stem for path in root.glob('*.db')
        if path.is_file() and not path.is_symlink()
        and not path.stem.startswith('registry-')
        and all(character.isalnum() or character in '-_' for character in path.stem)
    )


def _project_paths(root, name):
    root = root.absolute()
    if name not in project_names(root):
        raise ValueError('Unknown personal experiment project.')
    os.environ['TRACKIO_DIR'] = str(root)
    os.environ['TRACKIO_TRACE_SESSIONS_DIR'] = str(root / 'traces')
    from trackio.sqlite_storage import SQLiteStorage
    from trackio.utils import project_artifacts_dir, project_media_dir

    database = SQLiteStorage.get_project_db_path(name)
    parquets = SQLiteStorage._project_parquet_paths(database)
    for other_database in root.glob('*.db'):
        if other_database == database:
            continue
        other_exports = set(SQLiteStorage._project_parquet_paths(other_database))
        if any(path in other_exports and (path.exists() or path.is_symlink()) for path in parquets):
            raise ValueError('An export filename overlaps another project database.')
    paths = [database, Path(str(database) + '-wal'), Path(str(database) + '-shm'),
             *parquets, project_media_dir(name),
             project_artifacts_dir(name), root / 'traces' / name,
             root / f'{name}.hash', root / f'{name}.lock']
    # Validate every target before the first mutation; rmtree never follows nested links.
    for path in paths:
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('Project data points outside the personal directory.')
        for part in (path, *path.parents):
            if part.is_symlink():
                raise ValueError('Project data must not use symbolic links.')
            if part == root:
                break
    return paths


def delete_project_data(root, name):
    """Called only in the helper process, after the manager locks/stops access."""
    paths = _project_paths(root, name)
    from trackio import delete_project
    if not delete_project(name, force=True):
        raise RuntimeError('Trackio could not complete project deletion.')
    # Trackio ignores some rmtree errors and leaves traces/hash/lock files behind.
    # Retry/check cleanup explicitly so partial deletion is never reported as success.
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    if any(path.exists() or path.is_symlink() for path in paths):
        raise RuntimeError('Some project files remain after deletion.')


def share_project_data(root, name, destination):
    root, destination = root.absolute(), destination.absolute()
    paths = _project_paths(root, name)
    if not destination.is_relative_to(root / 'projects') or destination.exists():
        raise ValueError('The new team directory must be unused and inside the personal projects directory.')
    if any(path.is_symlink() for path in (destination, *destination.parents)):
        raise ValueError('The team directory must not use symbolic links.')
    for path in paths:
        if path.is_dir() and any(child.is_symlink() for child in path.rglob('*')):
            raise ValueError('Project data must not contain symbolic links.')
    moved, aliases = [], []
    destination.mkdir(parents=True, mode=0o700)
    try:
        for path in paths:
            if path.exists():
                target = destination / path.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                path.rename(target)
                moved.append((path, target))
        # Keep the owner's old URL reading/writing the same DB, media, and CAS data.
        directories = [root / folder / name for folder in ('media', 'artifacts', 'traces')]
        required = [root / f'{name}.db', root / f'{name}.db-wal', root / f'{name}.db-shm',
                    root / f'{name}.hash', root / f'{name}.lock',
                    *directories]
        for alias in dict.fromkeys([path for path, _ in moved] + required):
            target = destination / alias.relative_to(root)
            if alias in directories:
                target.mkdir(parents=True, exist_ok=True)
            alias.parent.mkdir(parents=True, exist_ok=True)
            alias.symlink_to(os.path.relpath(target, alias.parent), target_is_directory=target.is_dir())
            aliases.append(alias)
    except Exception:
        for alias in reversed(aliases):
            alias.unlink()
        for path, target in reversed(moved):
            target.rename(path)
        shutil.rmtree(destination)
        raise


def restore_shared_project(root, name, destination):
    """Undo a completed move if the management transaction could not commit."""
    root, destination = root.absolute(), destination.absolute()
    if not destination.is_relative_to(root / 'projects'):
        raise ValueError('Invalid team directory.')
    paths = _project_paths(destination, name)
    aliases = [root / path.relative_to(destination) for path in paths]
    for alias, target in zip(aliases, paths):
        if alias.is_symlink():
            if alias.resolve() != target.resolve():
                raise ValueError('The original project paths changed during conversion.')
        elif alias.exists():
            raise ValueError('The original project paths must not be overwritten.')
    for alias in aliases:
        if alias.is_symlink():
            alias.unlink()
    for path, alias in zip(paths, aliases):
        if path.exists():
            alias.parent.mkdir(parents=True, exist_ok=True)
            path.rename(alias)
    shutil.rmtree(destination)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('name')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--share-to', type=Path)
    action.add_argument('--restore-from', type=Path)
    args = parser.parse_args()
    try:
        if args.share_to:
            share_project_data(args.root, args.name, args.share_to)
        elif args.restore_from:
            restore_shared_project(args.root, args.name, args.restore_from)
        else:
            delete_project_data(args.root, args.name)
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
