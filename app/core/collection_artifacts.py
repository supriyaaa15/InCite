"""Process-shared fence for collection writes and physical cleanup."""

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import time
from uuid import UUID

from app.core.config import settings

if os.name == "nt":
    import msvcrt
else:
    import fcntl


@contextmanager
def collection_artifact_lock(collection_id: UUID, *, wait: bool = True):
    directory = Path(settings.CHROMA_PERSIST_PATH).resolve() / ".incite-artifact-locks"
    directory.mkdir(parents=True, exist_ok=True)
    # Keep lock files in place. Unlinking one would let a second process
    # create a different lock for the same collection.
    with (directory / f"{collection_id}.lock").open("a+b") as handle:
        while True:
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if not wait:
                    yield False
                    return
                time.sleep(0.05)
        # Closing the handle releases the OS lock, including on process exit.
        yield True
