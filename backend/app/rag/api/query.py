from fastapi import APIRouter, Depends
from app.rag.api.dependencies import get_pipeline
from app.rag.schemas import QueryRequest, QueryResponse, RetrievalResponse

router = APIRouter(prefix="/api/rag", tags=["RAG queries"])

@router.post("/retrieve", response_model=RetrievalResponse)
def retrieve(request: QueryRequest, pipeline=Depends(get_pipeline)):
    return RetrievalResponse(chunks=pipeline.retriever.retrieve(request))

@router.post("/query", response_model=QueryResponse)
async def query(request: QueryRequest, pipeline=Depends(get_pipeline)):
    return await pipeline.query(request)
