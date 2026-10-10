from fastapi import APIRouter, Request
from app.rag.schemas import HealthResponse
from app.rag.errors import RAGError

router = APIRouter(prefix="/api/rag", tags=["RAG health"])

@router.get("/health", response_model=HealthResponse)
def health(request: Request):
    settings = request.app.state.rag_settings
    pipeline = request.app.state.rag_pipeline
    storage = "not_initialized"
    if pipeline is not None:
        try:
            with pipeline.store.transaction():
                storage = "ready" if pipeline.store.chunks else "empty"
        except RAGError:
            storage = "unavailable_or_incompatible"
    return HealthResponse(enabled=settings.rag_enabled, embedding_model=settings.rag_embedding_model, embedding_loaded=bool(pipeline and getattr(pipeline.embeddings, "_model", None) is not None), llm_configured=bool(settings.llm_base_url and settings.llm_model), reranking_enabled=settings.rag_rerank_enabled, storage=storage)
