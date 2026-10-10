from fastapi import APIRouter, Depends, UploadFile, File, Response
from starlette.concurrency import run_in_threadpool
from app.rag.api.dependencies import get_pipeline
from app.rag.errors import RAGError
from app.rag.schemas import Document, IngestionResponse
from app.rag.ingestion.parser import SUPPORTED
from app.os_limits import trim_memory
from pathlib import Path

router = APIRouter(prefix="/api/rag/documents", tags=["RAG documents"])

@router.post("/upload", response_model=IngestionResponse, status_code=201)
async def upload_document(file: UploadFile = File(...), pipeline=Depends(get_pipeline)):
    try:
        if Path(file.filename or "").suffix.lower() not in SUPPORTED:
            raise RAGError("Unsupported document type", 415)
        limit = pipeline.settings.rag_max_upload_mb * 1024 * 1024
        data = bytearray()
        while block := await file.read(64 * 1024):
            data.extend(block)
            if len(data) > limit:
                raise RAGError("Upload exceeds configured size limit", 413)
        response = await run_in_threadpool(pipeline.loader.ingest, bytes(data), file.filename or "document")
        del data
        await run_in_threadpool(trim_memory)
        return response
    finally:
        await file.close()

@router.get("", response_model=list[Document])
def list_documents(pipeline=Depends(get_pipeline)):
    with pipeline.store.transaction():
        return list(pipeline.store.documents.values())

@router.get("/{document_id}", response_model=Document)
def get_document(document_id: str, pipeline=Depends(get_pipeline)):
    with pipeline.store.transaction():
        if document_id not in pipeline.store.documents:
            raise RAGError("Document not found", 404)
        return pipeline.store.documents[document_id]

@router.delete("/{document_id}", status_code=204)
def delete_document(document_id: str, pipeline=Depends(get_pipeline)):
    pipeline.loader.delete(document_id)
    trim_memory()
    return Response(status_code=204)
