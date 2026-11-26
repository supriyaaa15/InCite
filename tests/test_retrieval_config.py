import pytest
from pydantic import ValidationError

from app.core.config import Settings


def load_settings():
    return Settings(_env_file=None, JWT_SECRET="synthetic-test-secret", GROQ_API_KEY="synthetic-test-key")


def test_retrieval_mode_defaults_to_vector(monkeypatch):
    monkeypatch.delenv("RETRIEVAL_MODE", raising=False)
    assert load_settings().RETRIEVAL_MODE == "vector"


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ("vector", "vector"),
        ("hybrid", "hybrid"),
        ("hybrid_rerank", "hybrid_rerank"),
        ("HYBRID", "hybrid"),
        ("Hybrid_Rerank", "hybrid_rerank"),
    ],
)
def test_valid_retrieval_mode_environment_overrides(monkeypatch, override, expected):
    monkeypatch.setenv("RETRIEVAL_MODE", override)
    assert load_settings().RETRIEVAL_MODE == expected


@pytest.mark.parametrize("override", ["", " ", "hybird", "unknown"])
def test_invalid_retrieval_mode_reports_accepted_values(monkeypatch, override):
    monkeypatch.setenv("RETRIEVAL_MODE", override)
    with pytest.raises(ValidationError) as exc:
        load_settings()
    assert exc.value.errors()[0]["loc"] == ("RETRIEVAL_MODE",)
    assert all(mode in str(exc.value) for mode in ("vector", "hybrid", "hybrid_rerank"))
