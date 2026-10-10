import re
import pytest
from fastapi.testclient import TestClient
from app.rag.config import RAGSettings
from app.rag.embeddings.base import EmbeddingProvider
from app.rag.pipeline.rag_pipeline import RAGPipeline

class FakeEmbeddings(EmbeddingProvider):
    calls = 0
    @property
    def identity(self):
        return "test-embedding-v1"
    def embed_documents(self, texts):
        self.calls += 1
        return [[float(len(re.findall(r"pump", t.lower()))), float(len(re.findall(r"motor", t.lower()))), 0.1] for t in texts]
    def embed_query(self, text):
        return self.embed_documents([text])[0]

@pytest.fixture
def pipeline(tmp_path):
    settings = RAGSettings(_env_file=None, rag_data_dir=tmp_path, rag_chunk_size=100, rag_chunk_overlap=10)
    return RAGPipeline(settings, embeddings=FakeEmbeddings())

@pytest.fixture
def client(pipeline):
    from app.main import app
    previous = (app.state.rag_settings, app.state.rag_pipeline)
    app.state.rag_settings = pipeline.settings
    app.state.rag_pipeline = pipeline
    with TestClient(app) as client:
        yield client
    app.state.rag_settings, app.state.rag_pipeline = previous
