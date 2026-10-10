from threading import RLock
import numpy as np
from app.rag.embeddings.base import EmbeddingProvider
from app.rag.errors import RAGError
from app.os_limits import ensure_memory_for_model_load

class SentenceTransformerEmbeddings(EmbeddingProvider):
    def __init__(self, model: str, batch_size: int = 32):
        self.model_name, self.batch_size = model, batch_size
        self._model = None
        self._lock = RLock()

    @property
    def identity(self):
        return self.model_name

    def _encode(self, texts):
        with self._lock:
            if self._model is None:
                ensure_memory_for_model_load()
        try:
            with self._lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer
                    self._model = SentenceTransformer(self.model_name, device="cpu", trust_remote_code=False)
                vectors = self._model.encode(texts, batch_size=self.batch_size, normalize_embeddings=True, convert_to_numpy=True)
            return np.asarray(vectors, dtype=np.float32).tolist()
        except Exception as exc:
            raise RAGError("Embedding failed; check model availability and server resources") from exc

    def embed_documents(self, texts):
        return self._encode(list(texts)) if texts else []

    def embed_query(self, text):
        return self._encode([text])[0]
