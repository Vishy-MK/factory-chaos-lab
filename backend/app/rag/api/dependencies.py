from fastapi import Request
from app.rag.errors import RAGError

def get_pipeline(request: Request):
    if not request.app.state.rag_settings.rag_enabled:
        raise RAGError("RAG is disabled", 503)
    # Construction imports FAISS but never downloads or loads a model.
    with request.app.state.rag_lock:
        if request.app.state.rag_pipeline is None:
            from app.rag.pipeline.rag_pipeline import RAGPipeline
            request.app.state.rag_pipeline = RAGPipeline(request.app.state.rag_settings)
        return request.app.state.rag_pipeline
