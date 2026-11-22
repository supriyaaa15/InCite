"""The validated text and metadata carried through retrieval."""

from dataclasses import field

from pydantic import Field
from pydantic.dataclasses import dataclass


@dataclass
class RetrievedChunk:
    page_content: str
    metadata: dict = field(default_factory=dict, kw_only=True)
    id: str | None = Field(default=None, kw_only=True, coerce_numbers_to_str=True)
