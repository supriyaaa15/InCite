import os
from pathlib import Path
import shutil
import stat
import uuid

from fastapi import UploadFile

from app.core.config import settings


class UnsafeUploadPath(ValueError):
    pass


def _verify_storage_entry(path: Path, *, directory: bool) -> None:
    entry = path.lstat()
    linked = stat.S_ISLNK(entry.st_mode) or (
        getattr(entry, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )
    expected = stat.S_ISDIR(entry.st_mode) if directory else stat.S_ISREG(entry.st_mode) and entry.st_nlink == 1
    if linked or not expected:
        raise UnsafeUploadPath("Unexpected storage entry. Manual review required.")


class StorageService:
    """
    Every file operation in the app goes through this class — never touch
    the filesystem directly from routes or other services.

    Today: saves to local disk under UPLOAD_DIR/{user_id}/{collection_id}/.
    Later: swap the internals for boto3/S3 calls. Callers never change.
    """

    def _dir_for(self, user_id: str, collection_id: str) -> str:
        path = os.path.join(settings.UPLOAD_DIR, str(user_id), str(collection_id))
        os.makedirs(path, exist_ok=True)
        return path

    def save_file(self, user_id: str, collection_id: str, file: UploadFile) -> str:
        """Saves the uploaded file, returns the path it was stored at."""
        directory = self._dir_for(user_id, collection_id)
        # Prefix with a uuid to avoid filename collisions between uploads
        safe_name = f"{uuid.uuid4()}_{file.filename}"
        full_path = os.path.join(directory, safe_name)

        with open(full_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        return full_path

    def get_file(self, file_path: str) -> bytes:
        """Reads and returns the raw bytes of a stored file."""
        with open(file_path, "rb") as f:
            return f.read()

    def delete_file(self, file_path: str) -> None:
        """Removes a stored file. Safe to call even if it's already gone."""
        if os.path.exists(file_path):
            os.remove(file_path)

    def delete_document_pdf(
        self, user_id: uuid.UUID, collection_id: uuid.UUID, file_path: str, other_paths: list[str],
    ) -> None:
        root = Path(os.path.abspath(settings.UPLOAD_DIR))
        directory = root / str(user_id) / str(collection_id)
        candidate = Path(os.path.abspath(file_path))
        if (
            candidate.parent != directory or ".." in Path(file_path).parts
            or ":" in candidate.name or candidate.name.endswith((".", " "))
        ):
            raise UnsafeUploadPath("Stored PDF path is outside its collection directory.")
        if any(Path(os.path.abspath(other)) == candidate for other in other_paths):
            raise UnsafeUploadPath("Stored PDF is referenced by another document.")
        try:
            for parent in (root, directory.parent, directory):
                _verify_storage_entry(parent, directory=True)
            _verify_storage_entry(candidate, directory=False)
            candidate.unlink()
        except FileNotFoundError:
            return

    def remove_empty_collection_dir(self, user_id: uuid.UUID, collection_id: uuid.UUID) -> None:
        root = Path(os.path.abspath(settings.UPLOAD_DIR))
        directory = root / str(user_id) / str(collection_id)
        try:
            for parent in (root, directory.parent, directory):
                _verify_storage_entry(parent, directory=True)
            directory.rmdir()
        except FileNotFoundError:
            return


storage_service = StorageService()
