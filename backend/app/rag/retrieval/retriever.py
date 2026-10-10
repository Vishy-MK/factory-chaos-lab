from typing import Any
import numpy as np
from langchain_core.retrievers import BaseRetriever
from pydantic import ConfigDict
from app.rag.schemas import QueryRequest

class Retriever(BaseRetriever):
    """LangChain retriever over the FAISS store; retrieve() keeps scored hits for the API."""
    model_config = ConfigDict(arbitrary_types_allowed=True)
    settings: Any
    embeddings: Any
    store: Any

    def retrieve(self, request: QueryRequest):
        request = QueryRequest.model_validate(request)
        with self.store.transaction():
            if not self.store.chunks:
                return []
        vector = np.asarray([self.embeddings.embed_query(request.query)], dtype=np.float32)
        with self.store.transaction():
            return self.store.search(vector, request.top_k or self.settings.rag_top_k, request.document_ids, request.similarity_threshold if request.similarity_threshold is not None else self.settings.rag_similarity_threshold)

    def _get_relevant_documents(self, query, *, run_manager, top_k=None, document_ids=None, similarity_threshold=None):
        hits = self.retrieve(QueryRequest(query=query, top_k=top_k, document_ids=document_ids, similarity_threshold=similarity_threshold))
        return [hit.to_document() for hit in hits]
