import time
from typing import TypedDict
from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph
from starlette.concurrency import run_in_threadpool
from app.rag.embeddings.sentence_transformer import SentenceTransformerEmbeddings
from app.rag.vectorstore.faiss_store import FaissStore
from app.rag.ingestion.loader import DocumentLoader
from app.rag.retrieval.retriever import Retriever
from app.rag.retrieval.reranker import Reranker
from app.rag.generation.llm_provider import ChatModelProvider, OpenAICompatibleProvider
from app.rag.generation.prompts import RAG_PROMPT, SYSTEM_PROMPT, build_context, validate_citations
from app.rag.schemas import Citation, Hit, QueryResponse, QueryRequest
from app.rag.errors import RAGError

INSUFFICIENT_EVIDENCE = "There is insufficient document evidence to answer this question."

class RAGState(TypedDict, total=False):
    request: QueryRequest
    hits: list[Hit]
    context: str
    included: list[Hit]
    answer: str
    citations: list[Citation]

class RAGPipeline:
    def __init__(self, settings, embeddings=None, store=None, llm=None):
        self.settings = settings
        self.embeddings = embeddings or SentenceTransformerEmbeddings(settings.rag_embedding_model, settings.rag_embedding_batch_size)
        # The splitter tag makes indexes chunked by the previous custom splitter report as incompatible.
        self.store = store or FaissStore(settings.rag_index_dir, self.embeddings, dict(size=settings.rag_chunk_size, overlap=settings.rag_chunk_overlap, strategy=settings.rag_chunk_strategy, splitter="langchain"))
        self.loader = DocumentLoader(settings, self.embeddings, self.store)
        self.retriever = Retriever(settings=settings, embeddings=self.embeddings, store=self.store)
        self.llm = ChatModelProvider(llm) if isinstance(llm, BaseChatModel) else llm or OpenAICompatibleProvider(settings)
        self.reranker = Reranker(settings.rag_rerank_model) if settings.rag_rerank_enabled else None
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(RAGState)
        graph.add_node("retrieve", self._retrieve)
        graph.add_node("rerank", self._rerank)
        graph.add_node("build_context", self._build_context)
        graph.add_node("generate", self._generate)
        graph.add_node("insufficient_evidence", self._insufficient_evidence)
        graph.add_edge(START, "retrieve")
        graph.add_conditional_edges("retrieve", lambda state: "rerank" if self.reranker else "build_context", ["rerank", "build_context"])
        graph.add_edge("rerank", "build_context")
        graph.add_conditional_edges("build_context", lambda state: "generate" if state["included"] else "insufficient_evidence", ["generate", "insufficient_evidence"])
        graph.add_edge("generate", END)
        graph.add_edge("insufficient_evidence", END)
        return graph.compile()

    async def _retrieve(self, state: RAGState):
        return {"hits": await run_in_threadpool(self.retriever.retrieve, state["request"])}

    async def _rerank(self, state: RAGState):
        return {"hits": await run_in_threadpool(self.reranker.rerank, state["request"].query, state["hits"])}

    def _build_context(self, state: RAGState):
        context, included = build_context(state["hits"], self.settings.rag_max_context_chars)
        return {"context": context, "included": included}

    async def _generate(self, state: RAGState):
        messages = RAG_PROMPT.format_messages(system_prompt=self.settings.rag_system_prompt or SYSTEM_PROMPT, question=state["request"].query, context=state["context"])
        answer = await self.llm.generate(messages)
        return {"answer": answer, "citations": validate_citations(answer, state["included"])}

    def _insufficient_evidence(self, state: RAGState):
        return {"answer": INSUFFICIENT_EVIDENCE, "citations": []}

    async def query(self, request: QueryRequest):
        started = time.perf_counter()
        request = QueryRequest.model_validate(request)
        if not getattr(self.llm, "configured", True):
            raise RAGError("Generation requires LLM_BASE_URL and LLM_MODEL (and LLM_API_KEY if required)")
        state = await self.graph.ainvoke({"request": request})
        metadata = dict(retrieval_count=len(state["hits"]), context_source_count=len(state["included"]), latency_ms=round((time.perf_counter()-started)*1000, 2), reranking_enabled=self.reranker is not None)
        return QueryResponse(answer=state["answer"], citations=state["citations"], metadata=metadata)
