from abc import ABC, abstractmethod
from langchain_core.embeddings import Embeddings

class EmbeddingProvider(Embeddings, ABC):
    """LangChain Embeddings with a stable identity recorded in the index."""
    @property
    @abstractmethod
    def identity(self) -> str: ...
