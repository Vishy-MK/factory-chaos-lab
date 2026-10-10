from threading import RLock
from fastapi.responses import JSONResponse
from app.rag.config import RAGSettings
from app.rag.errors import RAGError
from app.rag.api import documents, query, health

def register_rag(app):
    app.state.rag_settings = RAGSettings()
    app.state.rag_pipeline = None
    app.state.rag_lock = RLock()

    @app.exception_handler(RAGError)
    async def rag_error_handler(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    for router in (documents.router, query.router, health.router):
        app.include_router(router)
