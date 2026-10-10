from pathlib import Path
from typing import Literal
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class RAGSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False, hide_input_in_errors=True)
    rag_enabled: bool = True
    rag_data_dir: Path = Path("./data/rag")
    rag_upload_dir: Path | None = None
    rag_index_dir: Path | None = None
    rag_embedding_model: str = Field("sentence-transformers/all-MiniLM-L6-v2", min_length=1)
    rag_chunk_size: int = Field(1000, ge=1)
    rag_chunk_overlap: int = Field(150, ge=0)
    rag_chunk_strategy: Literal["fixed", "paragraph"] = "paragraph"
    rag_embedding_batch_size: int = Field(32, ge=1, le=1024)
    rag_top_k: int = Field(5, ge=1, le=100)
    rag_max_context_chars: int = Field(12000, ge=128)
    rag_rerank_enabled: bool = False
    rag_rerank_model: str = ""
    rag_max_upload_mb: int = Field(25, ge=1, le=1024)
    rag_similarity_threshold: float | None = Field(None, ge=-1, le=1)
    rag_system_prompt: str | None = None
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = Field(45, gt=0)
    llm_max_retries: int = Field(2, ge=0, le=5)

    @model_validator(mode="after")
    def validate_settings(self):
        if self.rag_chunk_overlap >= self.rag_chunk_size:
            raise ValueError("RAG_CHUNK_OVERLAP must be smaller than RAG_CHUNK_SIZE")
        if self.rag_rerank_enabled and not self.rag_rerank_model:
            raise ValueError("RAG_RERANK_MODEL is required when reranking is enabled")
        if self.llm_base_url and not self.llm_base_url.startswith(("http://", "https://")):
            raise ValueError("LLM_BASE_URL must be an HTTP(S) URL")
        self.rag_upload_dir = self.rag_upload_dir or self.rag_data_dir / "uploads"
        self.rag_index_dir = self.rag_index_dir or self.rag_data_dir / "index"
        return self
