"""Small file-system helpers shared by the logserver package and log-master."""

import os
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically replace `path` with `data`.

    Writes `<path>.tmp`, fsyncs it, renames it over `path`, and fsyncs the parent directory
    so the rename itself is durable. Readers see either the old or the new content, never a
    partial file.
    """
    path = Path(path)
    tmp_path = path.with_name(path.name + '.tmp')
    with open(tmp_path, 'wb') as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp_path, path)
    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
