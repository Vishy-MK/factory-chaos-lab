import hashlib
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from app.rag.errors import RAGError
from app.rag.ingestion.parser import parse_document
from app.rag.ingestion.chunker import chunk_sections
from app.rag.schemas import Document, IngestionResponse

class DocumentLoader:
    def __init__(self, settings, embeddings, store):
        self.settings, self.embeddings, self.store = settings, embeddings, store

    def ingest(self, data: bytes, filename: str):
        settings = self.settings
        if len(data) > settings.rag_max_upload_mb * 1024 * 1024:
            raise RAGError("Upload exceeds configured size limit", 413)
        filename = Path(filename.replace("\\", "/")).name[:255]
        extension = Path(filename).suffix.lower()
        sections = parse_document(data, extension)
        digest = hashlib.sha256(data).hexdigest()
        with self.store.transaction():
            for existing in self.store.documents.values():
                if existing.content_hash == digest:
                    return IngestionResponse(document=existing, deduplicated=True)
        document_id = "doc_" + uuid.uuid4().hex
        info = dict(document_id=document_id, filename=filename, document_type=extension.lstrip("."), content_hash=digest)
        chunks = chunk_sections(sections, info, settings.rag_chunk_size, settings.rag_chunk_overlap, settings.rag_chunk_strategy)
        if not chunks:
            raise RAGError("Document contains no indexable text", 422)
        vectors = self.embeddings.embed_documents([c.text for c in chunks])
        document = Document(**info, chunk_count=len(chunks), uploaded_at=datetime.now(timezone.utc).isoformat())
        with self.store.transaction():
            for existing in self.store.documents.values():
                if existing.content_hash == digest:
                    return IngestionResponse(document=existing, deduplicated=True)
            settings.rag_upload_dir.mkdir(parents=True, exist_ok=True)
            target = settings.rag_upload_dir / (document_id + ".bin")
            try:
                with target.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                self.store.insert(document, chunks, vectors)
            except Exception:
                # A failure after publication must not remove an indexed source.
                self.store._load()
                if document_id not in self.store.documents:
                    target.unlink(missing_ok=True)
                raise
        return IngestionResponse(document=document)

    def delete(self, document_id):
        with self.store.transaction():
            self.store.delete(document_id)
            (self.settings.rag_upload_dir / (document_id + ".bin")).unlink(missing_ok=True)
