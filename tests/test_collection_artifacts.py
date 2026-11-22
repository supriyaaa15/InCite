import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from app.core.collection_artifacts import collection_artifact_lock
from app.core.config import settings


def test_artifact_lock_excludes_another_process(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "CHROMA_PERSIST_PATH", str(tmp_path))
    collection_id = uuid4()
    env = {**os.environ, "CHROMA_PERSIST_PATH": str(tmp_path),
           "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    command = [sys.executable, "-B", "-s", "-c", """
import os
import sys
from uuid import UUID
from app.core.collection_artifacts import collection_artifact_lock
with collection_artifact_lock(UUID(sys.argv[1]), wait=False) as acquired:
    if len(sys.argv) > 2:
        assert acquired
        os._exit(0)
    print(acquired)
""", str(collection_id)]

    def try_from_child():
        return subprocess.run(command, cwd=tmp_path, env=env, check=True, text=True,
                              capture_output=True, timeout=30).stdout.strip()

    with collection_artifact_lock(collection_id):
        assert try_from_child() == "False"
    assert try_from_child() == "True"
    subprocess.run([*command, "exit"], cwd=tmp_path, env=env, check=True, timeout=30)
    with collection_artifact_lock(collection_id, wait=False) as acquired:
        assert acquired
