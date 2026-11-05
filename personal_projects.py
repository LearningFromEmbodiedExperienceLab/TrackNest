"""Offline personal-project operations; Trackio deletion runs in a fresh process."""
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


def delete_project_data(root, name):
    """Called only in the helper process, after the manager locks/stops access."""
    root = root.absolute()
    if name not in project_names(root):
        raise ValueError('Unknown personal experiment project.')
    os.environ['TRACKIO_DIR'] = str(root)
    os.environ['TRACKIO_TRACE_SESSIONS_DIR'] = str(root / 'traces')
    from trackio import delete_project
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


if __name__ == '__main__':
    try:
        delete_project_data(Path(sys.argv[1]), sys.argv[2])
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
