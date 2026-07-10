import os
import shutil
import uuid

from fastapi import UploadFile

from app.core.config import settings


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


storage_service = StorageService()
