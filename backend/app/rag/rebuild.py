"""Offline rebuild: python -m app.rag.rebuild (stop the service first)."""
import hashlib
import json
import os
import re
import shutil
import tempfile
from filelock import FileLock
from app.rag.config import RAGSettings
from app.rag.errors import RAGError
from app.rag.schemas import Document
from app.rag.ingestion.parser import parse_document
from app.rag.ingestion.chunker import chunk_sections
from app.rag.pipeline.rag_pipeline import RAGPipeline


def rebuild(settings, embeddings=None):
    directory = settings.rag_index_dir
    directory.mkdir(parents=True, exist_ok=True)
    with FileLock(str(directory / "store.lock"), timeout=120):
        pointer = directory / "CURRENT"
        if not pointer.exists():
            raise RAGError("No persisted index exists to rebuild")
        generation = pointer.read_text().strip()
        if not re.fullmatch(r"gen_[0-9a-f]{32}", generation):
            raise RAGError("Invalid index manifest")
        old_folder = directory / generation
        metadata = json.loads((old_folder / "metadata.json").read_text())
        documents = [Document.model_validate(d) for d in metadata["documents"]]
        with tempfile.TemporaryDirectory(prefix="rebuild_", dir=directory) as temporary:
            staged_settings = settings.model_copy(update={"rag_index_dir": __import__("pathlib").Path(temporary)})
            pipeline = RAGPipeline(staged_settings, embeddings=embeddings)
            with pipeline.store.transaction():
                for document in documents:
                    if not re.fullmatch(r"doc_[0-9a-f]{32}", document.document_id):
                        raise RAGError("Invalid persisted document identifier")
                    data = (settings.rag_upload_dir / (document.document_id + ".bin")).read_bytes()
                    if hashlib.sha256(data).hexdigest() != document.content_hash:
                        raise RAGError("Uploaded source checksum mismatch; restore the original upload")
                    sections = parse_document(data, "." + document.document_type)
                    info = document.model_dump(exclude={"chunk_count", "uploaded_at"})
                    chunks = chunk_sections(sections, info, settings.rag_chunk_size, settings.rag_chunk_overlap, settings.rag_chunk_strategy)
                    vectors = pipeline.embeddings.embed_documents([chunk.text for chunk in chunks])
                    pipeline.store.insert(document.model_copy(update={"chunk_count": len(chunks)}), chunks, vectors)
                if not documents:
                    import faiss
                    pipeline.store.index = faiss.IndexFlatIP(1)
                    pipeline.store._save()
            new_generation = (pipeline.store.directory / "CURRENT").read_text().strip()
            # Keep an explicit backup untouched by automatic snapshot cleanup.
            backup = directory / "backups" / generation
            backup.parent.mkdir(exist_ok=True)
            if not backup.exists():
                shutil.copytree(old_folder, backup)
            os.replace(pipeline.store.directory / new_generation, directory / new_generation)
            pipeline.store._sync_directory(directory)
            temporary_pointer = directory / "CURRENT.tmp"
            with temporary_pointer.open("w") as stream:
                stream.write(new_generation)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_pointer, pointer)
            pipeline.store._sync_directory(directory)
    return len(documents)


if __name__ == "__main__":
    try:
        count = rebuild(RAGSettings())
        print(f"Rebuilt {count} documents. Previous snapshot preserved in index/backups.")
    except (RAGError, OSError, ValueError) as exc:
        raise SystemExit("Rebuild failed; original CURRENT is retained. Check source uploads, permissions, and index metadata.") from exc
