from hashlib import sha256
from langchain_core.documents import Document as LCDocument
from langchain_text_splitters import CharacterTextSplitter, RecursiveCharacterTextSplitter, TextSplitter
from app.rag.schemas import Chunk

def make_splitter(size: int, overlap: int, strategy: str = "paragraph") -> TextSplitter:
    if size < 1 or overlap < 0 or overlap >= size:
        raise ValueError("Require chunk size > overlap >= 0")
    if strategy == "paragraph":
        # Prefer paragraph, then line, then word boundaries before splitting characters.
        return RecursiveCharacterTextSplitter(chunk_size=size, chunk_overlap=overlap, separators=["\n\n", "\n", " ", ""], keep_separator="end", add_start_index=True)
    if strategy == "fixed":
        return CharacterTextSplitter(separator="", chunk_size=size, chunk_overlap=overlap, add_start_index=True)
    raise ValueError("Unknown chunk strategy")

def split_text(text: str, size: int, overlap: int, strategy: str = "paragraph") -> list[str]:
    splitter = make_splitter(size, overlap, strategy)
    return [piece for piece in splitter.split_text(text) if piece.strip()]

def chunk_sections(sections: list[LCDocument], document: dict, size: int, overlap: int, strategy: str) -> list[Chunk]:
    chunks = []
    for piece in make_splitter(size, overlap, strategy).split_documents(sections):
        if not piece.page_content.strip():
            continue
        metadata = dict(piece.metadata)
        page_number = metadata.pop("page_number", None)
        index = len(chunks)
        identity = f"{document['document_id']}:{index}:{piece.page_content}"
        chunks.append(Chunk(**document, chunk_id="chunk_" + sha256(identity.encode()).hexdigest()[:32], chunk_index=index, text=piece.page_content, page_number=page_number, metadata=metadata))
    return chunks
