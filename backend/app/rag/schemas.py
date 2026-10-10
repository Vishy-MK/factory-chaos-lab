from typing import Any
from langchain_core.documents import Document as LCDocument
from pydantic import BaseModel, Field, field_validator

class Document(BaseModel):
    document_id: str
    filename: str
    document_type: str
    content_hash: str
    chunk_count: int
    uploaded_at: str

class Chunk(BaseModel):
    chunk_id: str
    document_id: str
    filename: str
    document_type: str
    content_hash: str
    page_number: int | None = None
    chunk_index: int
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_document(self) -> LCDocument:
        """LangChain view of a chunk; the chunk text becomes page_content."""
        return LCDocument(id=self.chunk_id, page_content=self.text, metadata=self.model_dump(exclude={"text"}))

class Hit(Chunk):
    score: float
    rank: int

class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=10000)
    top_k: int | None = Field(None, ge=1, le=100)
    document_ids: list[str] | None = Field(None, max_length=100)
    similarity_threshold: float | None = Field(None, ge=-1, le=1)

    @field_validator("query")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("query must contain text")
        return value.strip()

class Citation(BaseModel):
    document_id: str
    chunk_id: str
    filename: str
    page_number: int | None = None

class QueryResponse(BaseModel):
    answer: str
    citations: list[Citation]
    metadata: dict[str, Any]

class IngestionResponse(BaseModel):
    document: Document
    deduplicated: bool = False

class RetrievalResponse(BaseModel):
    chunks: list[Hit]

class HealthResponse(BaseModel):
    enabled: bool
    embedding_model: str
    embedding_loaded: bool
    llm_configured: bool
    reranking_enabled: bool
    storage: str
