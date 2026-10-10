# TeamSynapse backend with reusable RAG

The original FastAPI entry point is **`app.main:app`**. `/health`, `/api/v1/predict`, CORS, and `InferenceTimingMiddleware` remain in place. The generic RAG engine adds routers to that application. No domain-specific knowledge is built into the engine.

## Architecture and files

```text
backend/
  app/main.py                 existing application and RAG registration
  app/middleware.py           existing timing middleware
  app/rag/
    config.py, schemas.py, errors.py
    ingestion/                PDF, DOCX, UTF-8 text/Markdown, CSV to LangChain Documents; LangChain text splitters
    embeddings/               LangChain Embeddings with lazy CPU Sentence Transformers
    vectorstore/              LangChain VectorStore over persisted FAISS IndexFlatIP
    retrieval/                LangChain BaseRetriever and optional cross-encoder
    generation/               LangChain chat models (ChatOpenAI), prompt template, citation validation
    pipeline/                 RAGPipeline: LangGraph StateGraph for queries
    evaluation/               recall@k and reciprocal rank helpers
    api/                      document, retrieval, query, health routers
    rebuild.py                offline rebuild with backup
  tests/                      deterministic embeddings and mocked LLM calls
  .env.example
  requirements.txt, requirements-test.txt
  deploy.sh
  data/rag/                   default runtime location, ignored by Git
```

Upload → parse into LangChain `Document`s → `RecursiveCharacterTextSplitter` (`paragraph`) or `CharacterTextSplitter` (`fixed`) → batch `embed_documents` → locked FAISS/JSON snapshot. Queries run through a compiled LangGraph `StateGraph`: `retrieve` → `rerank` (only when enabled) → `build_context` → `generate`, or `insufficient_evidence` when no context fits. `generate` formats a `ChatPromptTemplate`, calls the chat model, and validates citations. `RAGPipeline` accepts any LangChain `Embeddings` with an `identity`, a vector store, and either a LangChain `BaseChatModel` or a provider object for other applications or tests. `pipeline.retriever` is a standard LangChain retriever (`retriever.invoke("query", top_k=3)`), and `pipeline.store` supports `similarity_search`/`similarity_search_with_score`. Retrieval never needs an LLM.

## Local setup

Use Python **3.10–3.12**; the original ONNX Runtime pin limits newer Python compatibility. Linux x86_64 is the deployment target tested here. On a second machine:

```bash
cd TeamSynapse/backend
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
# Install CPU-only torch first to avoid pulling CUDA packages on Linux.
python -m pip install torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu
python -m pip install -r requirements.txt
# Only copy if .env does not already exist. Never overwrite existing secrets.
test -e .env || cp .env.example .env
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

On Windows use `py -3.12 -m venv venv` and `venv\Scripts\activate`; the Python install/start commands are otherwise the same. `deploy.sh` is Linux-only. Wheel availability on other CPU architectures must be checked before deployment.

Model loading happens on the first nonempty ingestion or retrieval, never at import or just to request health. Sentence Transformers downloads the configured model to the Hugging Face cache (normally `~/.cache/huggingface`). For servers without internet, pre-cache the model under the service user or set `RAG_EMBEDDING_MODEL` to a local model directory. Configure `HF_HOME` in the service environment if a different writable cache is needed. Model files are separate from RAG persistence. Loading fails with a useful error if downloads or resources are unavailable.

## Configuration

Pydantic Settings reads `.env` from the process working directory; environment variables override it. Existing unrelated `.env` keys are accepted. No credentials are supplied in this repository.

| Variable | Default | Purpose |
|---|---|---|
| `RAG_ENABLED` | `true` | `false` disables RAG operations; original endpoints still run |
| `RAG_DATA_DIR` | `./data/rag` | Runtime data root |
| `RAG_UPLOAD_DIR` | `<RAG_DATA_DIR>/uploads` | Server-named source files |
| `RAG_INDEX_DIR` | `<RAG_DATA_DIR>/index` | FAISS, JSON, manifest, lock |
| `RAG_EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | Model identity or local path |
| `RAG_CHUNK_SIZE` | `1000` | Maximum characters per chunk |
| `RAG_CHUNK_OVERLAP` | `150` | Overlap characters, less than size |
| `RAG_CHUNK_STRATEGY` | `paragraph` | `paragraph` or `fixed` |
| `RAG_EMBEDDING_BATCH_SIZE` | `32` | Encode batch size |
| `RAG_TOP_K` | `5` | Default results, 1–100 |
| `RAG_MAX_CONTEXT_CHARS` | `12000` | Evidence budget including source labels and JSON escaping |
| `RAG_SIMILARITY_THRESHOLD` | unset | Optional cosine cutoff, −1 to 1 |
| `RAG_RERANK_ENABLED` | `false` | Enable optional lazy cross-encoder |
| `RAG_RERANK_MODEL` | empty | Required when reranking is enabled |
| `RAG_MAX_UPLOAD_MB` | `25` | Document limit in MiB |
| `RAG_SYSTEM_PROMPT` | built-in grounding prompt | Complete replacement prompt; retain citation and untrusted-evidence rules |
| `LLM_API_KEY` | empty | Provider secret; optional for unauthenticated local servers |
| `LLM_BASE_URL` | empty | Explicit API root, e.g. `https://provider.example/v1` |
| `LLM_MODEL` | empty | Provider model name |
| `LLM_TIMEOUT_SECONDS` | `45` | Timeout per HTTP operation |
| `LLM_MAX_RETRIES` | `2` | Bounded retries on transport errors, 429, and 5xx (OpenAI client retry policy) |

Generation uses LangChain `ChatOpenAI`, which posts to `<LLM_BASE_URL>/chat/completions`. With `LLM_API_KEY` empty, a placeholder bearer token is sent, which unauthenticated local servers ignore. Both base URL and model are required for generation. Ingestion and retrieval work with all LLM fields empty; `/query` returns HTTP 503 with a configuration message. Request fields `top_k`, `document_ids`, and `similarity_threshold` override retrieval defaults. An empty filter list matches no documents.

Chunk sizes are **characters, not tokens**. Unicode, paragraph separators, long paragraphs, overlap, and PDF page boundaries are handled. Embedding models can truncate input beyond their token limit: choose chunk sizes for your model and language. CSV uses UTF-8 comma-separated rows with a header; row numbers and headers are carried in chunk metadata. DOCX preserves paragraph/table order but does not extract embedded images. PDF preserves physical page numbers; image-only PDFs return an OCR-required error. OCR is not included. Exact duplicate file hashes reuse the existing indexed document; the first filename is retained.

## API examples

All examples use the same existing backend on port 8000. Visit `/docs` for the schemas. Replace the IDs with values returned by upload.

```bash
# Existing endpoints
curl http://localhost:8000/health
curl -X POST http://localhost:8000/api/v1/predict \
  -H 'Content-Type: application/json' -d '{"data":[1,2,3]}'

# POST /api/rag/documents/upload: PDF, DOCX, TXT, MD, Markdown, CSV
printf 'The pump must be inspected every week.\n' > /tmp/sample.txt
curl -X POST http://localhost:8000/api/rag/documents/upload \
  -F 'file=@/tmp/sample.txt'

# GET /api/rag/documents
curl http://localhost:8000/api/rag/documents

# GET /api/rag/documents/{document_id}
curl http://localhost:8000/api/rag/documents/doc_REPLACE_ME

# POST /api/rag/retrieve: no LLM or key needed
curl -X POST http://localhost:8000/api/rag/retrieve \
  -H 'Content-Type: application/json' \
  -d '{"query":"How often should the pump be inspected?","top_k":5}'

# Optional document filter and cutoff
curl -X POST http://localhost:8000/api/rag/retrieve \
  -H 'Content-Type: application/json' \
  -d '{"query":"pump inspection","document_ids":["doc_REPLACE_ME"],"similarity_threshold":0.3}'

# POST /api/rag/query: configure LLM fields, then restart the backend
curl -X POST http://localhost:8000/api/rag/query \
  -H 'Content-Type: application/json' \
  -d '{"query":"How often should the pump be inspected?"}'

# GET /api/rag/health: never loads an embedding model
curl http://localhost:8000/api/rag/health

# DELETE /api/rag/documents/{document_id}: success is HTTP 204
curl -i -X DELETE http://localhost:8000/api/rag/documents/doc_REPLACE_ME
```

Upload responses include document metadata and a deduplication flag. Retrieval includes source IDs, page numbers where available, text, original cosine scores, rank, and row metadata. Query responses include `answer`, validated `citations`, and measured latency/retrieval metadata. Only context sources actually sent to the LLM may be cited; unknown or missing citations cause HTTP 502. This conservative policy can also reject an LLM's uncited insufficiency response when sources were provided. With no retrieved/context sources, the pipeline returns an explicit insufficiency response without calling the LLM.

Similarity scores are not calibrated confidence. A default top-k search can retrieve weak matches: calibrate `RAG_SIMILARITY_THRESHOLD` on your own corpus. Reranking reorders candidates but preserves cosine scores; it does not turn them into confidence. Valid source IDs do not prove that a generated claim is correct. Prompt defenses reduce document instruction injection risk but are not a security guarantee.

Errors: unsupported extension 415; oversized upload 413; malformed/empty document or query 422; absent document 404; disabled/misconfigured components or storage/embedding failure 503; malformed/rejected LLM response 502; LLM transport timeout 504. An empty index produces an empty retrieval result. Client errors contain no internal paths or stack traces.

## Persistence, consistency, and rebuild

Uploads use server-generated `doc_<uuid>.bin` names; the original name is display metadata only. FAISS vectors and JSON document/chunk metadata reside in an immutable generation directory. `CURRENT` is atomically replaced after files are flushed. SHA-256 checks and count/dimension/model/chunk-configuration checks detect inconsistencies. No pickle deserialization is used. FAISS files are native binaries and must come only from this trusted local storage; checksums detect accidental corruption, not a malicious local writer.

Every read/write reloads under the same cross-process file lock, preventing stale worker writes. Delete rebuilds vectors in order and publishes a new generation. A reader sees one complete generation. Keep data on a **local filesystem** with working OS file locks and atomic rename; network filesystems and distributed replicas are unsupported. Full snapshot writes and filter searches are linear in corpus size: use a managed vector database for larger systems. Ordinary uploads are durable before index publication. A crash between saving an upload and publishing the index may leave an orphan `.bin`; a failed deletion may leave an unused source file. These do not appear as indexed documents. Index snapshots are crash-safe under local filesystem guarantees; backups and source files need operational backup management. Only the newest regular snapshot is retained; explicit rebuild backups are retained separately.

Changing model identity or chunk configuration produces an incompatibility error instead of mixing vectors. Indexes built before the LangChain migration used a different splitter and are reported as incompatible; run the rebuild below once to re-chunk them. Changing the model behind the same name/path is not automatically detected; use immutable model versions/paths and rebuild deliberately.

To rebuild while preserving document IDs and original uploads:

```bash
cd TeamSynapse/backend
sudo systemctl stop bosch-backend  # only on a deployed machine
# Edit .env for the new embedding/chunk configuration before rebuilding.
source venv/bin/activate
python -m app.rag.rebuild
sudo systemctl start bosch-backend
```

The command stages an entirely new index and swaps `CURRENT` only after success. It checks source hashes and preserves the previous snapshot under `index/backups/<generation>`. It requires readable JSON document metadata and original uploads. If those are lost, restore backups or start with a new `RAG_INDEX_DIR` and reupload your source documents. A failed rebuild leaves the previous pointer intact. Take full data backups before operational changes. Avoid rebuilding with running workers: although the lock serializes storage, old workers still use the old model/configuration. For rollback, stop the service, restore the previous settings, copy the backup generation into the index directory, and restore its generation name as `CURRENT` before restarting.

## Linux deployment and worker memory

`deploy.sh` retains `bosch-backend`, `app.main:app`, port 8000, Gunicorn/Uvicorn workers, virtualenv installation, and the original dependencies. It installs CPU-only torch first, validates the worker override, and restarts the service on rerun so changes take effect. It never overwrites `.env` or deletes runtime data. The default remains **four workers**. Use:

```bash
cd TeamSynapse/backend
GUNICORN_WORKERS=1 bash deploy.sh
```

Each worker lazily loads and reuses its own embedding model. Python models are not shared between workers. Four active workers can consume approximately four times the model memory; reranking adds another model per active worker. One worker is recommended for a memory-constrained hackathon machine. File locking makes writes safe even when four workers are retained; transaction timeouts return 503. Parsing, embedding, and local storage operations run outside the async event loop.

`.env` is read from the backend working directory under systemd. Put your LLM keys there and restrict its permissions (`chmod 600 .env` under the service user), or use a systemd environment override. The script does not print secrets. For writable storage outside a read-only source checkout, set absolute paths, for example `RAG_DATA_DIR=/var/lib/teamsynapse/rag`, and provision the directory for the service's `User=` account:

```bash
sudo install -d -o YOUR_SERVICE_USER -g YOUR_SERVICE_GROUP -m 750 /var/lib/teamsynapse/rag
```

Ensure the service user also has a writable model cache. Gunicorn's retained 120-second worker timeout can be exceeded by a first model download/large ingestion; pre-cache the model before starting production. The RAG API follows the backend's existing unauthenticated access model. Put it behind your application's authentication/gateway for controlled use, and configure reverse-proxy request limits: Starlette may spool multipart uploads before endpoint size validation.

## Low-memory machines: OS-level resource injection

Resource limits can be injected at two levels:

- **Kernel (cgroup v2).** These are hard caps. `scripts/run_constrained.sh` runs the backend in a transient `systemd-run --user --scope` with `MemoryMax`, `MemoryHigh` (90% of max), `MemorySwapMax=0`, `CPUQuota` and `TasksMax`. `deploy.sh` writes the same limits into the service when `MEMORY_MAX`, `MEMORY_HIGH` or `CPU_QUOTA` are set.
- **Process (`app/os_limits.py`, applied before numpy/torch/faiss load).** These control how the app behaves inside the cap.

| Variable | Default | Effect |
|---|---|---|
| `OS_THREADS` | unset (`1` in the launcher) | Sets OpenMP/MKL/OpenBLAS thread counts and disables tokenizer parallelism |
| `OS_CPU_AFFINITY` | unset | Pins workers to CPUs, e.g. `0-1,3` |
| `OS_NICE` | `0` | Lowers scheduling priority (0–19) |
| `OS_MIN_AVAILABLE_MB` | `0` (`64` in the launcher) | Returns 503 with `Retry-After` for upload/retrieve/query when free cgroup memory is below this |
| `OS_MODEL_LOAD_MB` | `0` (`300` in the launcher) | Refuses to load the embedding model (503) when it would not fit, instead of being OOM-killed mid-load |

"Free" memory is the cgroup limit minus the working set (reclaimable page cache excluded), capped by system available memory. `/health` reports this under `memory` (RSS, cgroup limit, working set, peak, OOM-kill count) along with the applied `os_limits`. After uploads and deletes, the heap is trimmed (`malloc_trim`), which returns about 80 MB to the OS after a large ingestion.

```bash
# Run one constrained instance on port 8100
MEMORY_MAX=600M CPU_QUOTA=100% OS_THREADS=1 bash scripts/run_constrained.sh

# Measure several profiles (MEMORY_MAX:CPU_QUOTA:OS_THREADS) with the real model
venv/bin/python scripts/resource_probe.py --profile 600M:100%:1 --profile 500M:100%:1

# Deploy with limits for the whole service
GUNICORN_WORKERS=1 MEMORY_MAX=700M CPU_QUOTA=200% OS_THREADS=2 OS_MIN_AVAILABLE_MB=64 OS_MODEL_LOAD_MB=300 bash deploy.sh
```

Measured on this machine (4 cores, `all-MiniLM-L6-v2`, one worker, 400-paragraph document, 30 retrievals):

| Profile | Upload | Retrieval p50 | Peak | OOM kills |
|---|---|---|---|---|
| 1G, 100% CPU | 201, 13 s | 15 ms | 505 MB | 0 |
| 600M, 100% CPU | 201, 12 s | 16 ms | 514 MB | 0 |
| 550M, 100% CPU | 201, 15 s | 17 ms | 506 MB | 0 |
| 500M, 100% CPU | 503 (model would not fit) | 2 ms (empty index) | 219 MB | 0 |
| 600M, 25% CPU | 201, 53 s | 98 ms | 506 MB | 0 |

Where the memory goes in one worker: about 54 MB idle, about 218 MB once the RAG pipeline is built, and roughly 300 MB more for loading the model and batch encoding. `langchain_core.language_models` imports `transformers`, and through it `torch`, at import time whenever transformers is installed, so torch loads with the pipeline rather than with the first embedding. **Plan for about 550 MB per worker.** `MEMORY_MAX` applies to the whole service, so four workers need about 2.2 GB. Without the preflight, a 400 MB cap made the kernel OOM-kill the worker during model load, and Gunicorn respawned it into the same crash on every request. Lowering `RAG_EMBEDDING_BATCH_SIZE` reduces the encoding peak. A CPU quota mostly costs latency, not memory.

## Tests and troubleshooting

```bash
cd TeamSynapse/backend
source venv/bin/activate
python -m pip install -r requirements-test.txt
python -m pytest -q
python -m pip check
bash -n deploy.sh
```

Unit tests use deterministic fake embeddings, generated parser fixtures, and mocked HTTP/LLM calls; no real API key or model download is needed. Existing prediction/health behavior, timing headers, and CORS are exercised. There were no pre-existing tests in this repository.

- **Embedding failure:** check model/cache permissions, internet access on first load, CPU torch installation, RAM, and model identity.
- **Index incompatible/unavailable:** restore original model/chunk settings or use the rebuild procedure; inspect directory permissions. Health reports storage status without revealing paths.
- **No useful retrieval:** verify documents are listed, clear restrictive filters, calibrate the threshold, and use an appropriate multilingual model for non-English data.
- **OCR required:** run a real OCR tool externally and upload its extracted text; no OCR results are simulated.
- **Generation 503:** set base URL and model, supply a key if the provider requires one, and restart.
- **Generation 502/504:** check the provider's chat-completion compatibility, limits, availability, and timeout; responses require exact `[chunk_...]` source IDs.
- **Memory pressure:** reduce workers, batch size, document size, or choose a smaller embedding/reranking model.

Implementation references: [LangChain text splitters](https://python.langchain.com/docs/concepts/text_splitters/), [LangGraph](https://langchain-ai.github.io/langgraph/), [Sentence Transformers model API](https://www.sbert.net/docs/package_reference/sentence_transformer/model.html), [FAISS IndexFlatIP](https://faiss.ai/cpp_api/struct/structfaiss_1_1IndexFlatIP.html), [Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/).
