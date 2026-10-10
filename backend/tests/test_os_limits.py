import os
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app import os_limits
from app.middleware import MemoryGuardMiddleware
from app.os_limits import OSLimitSettings, apply_os_limits, ensure_memory_for_model_load, memory_status, parse_cpu_list
from app.rag.errors import RAGError

def test_parse_cpu_list():
    assert parse_cpu_list("0") == {0}
    assert parse_cpu_list("0-2, 5") == {0, 1, 2, 5}
    with pytest.raises(ValidationError):
        OSLimitSettings(_env_file=None, os_cpu_affinity="a-b")

def test_apply_threads_and_affinity(monkeypatch):
    # setenv records the previous state, so the values written below are undone after the test.
    for name in (*os_limits.THREAD_VARIABLES, "TOKENIZERS_PARALLELISM"):
        monkeypatch.setenv(name, "")
    original = os.sched_getaffinity(0)
    first = min(original)
    try:
        applied = apply_os_limits(OSLimitSettings(_env_file=None, os_threads=2, os_cpu_affinity=str(first)))
        assert applied["threads"] == 2 and os.environ["OMP_NUM_THREADS"] == "2"
        assert os.environ["TOKENIZERS_PARALLELISM"] == "false"
        assert applied["cpu_affinity"] == [first] and os.sched_getaffinity(0) == {first}
        with pytest.raises(ValueError):
            apply_os_limits(OSLimitSettings(_env_file=None, os_cpu_affinity="4096"))
    finally:
        os.sched_setaffinity(0, original)

def test_memory_status_shape():
    status = memory_status()
    assert status["process_rss_mb"] > 0 and status["available_mb"] <= status["system_available_mb"]

def test_model_load_preflight(monkeypatch):
    monkeypatch.setattr(os_limits, "memory_status", lambda: {"available_mb": 100})
    monkeypatch.setenv("OS_MODEL_LOAD_MB", "300")
    with pytest.raises(RAGError) as error:
        ensure_memory_for_model_load()
    assert error.value.status_code == 503
    monkeypatch.setenv("OS_MODEL_LOAD_MB", "50")
    ensure_memory_for_model_load()

def test_memory_guard_sheds_heavy_requests(monkeypatch):
    app = FastAPI()
    app.add_middleware(MemoryGuardMiddleware, min_available_mb=200)
    @app.post("/api/rag/retrieve")
    def retrieve():
        return {"ok": True}
    @app.get("/api/rag/health")
    def health():
        return {"ok": True}
    client = TestClient(app)
    monkeypatch.setattr(os_limits, "memory_status", lambda: {"available_mb": 100})
    shed = client.post("/api/rag/retrieve")
    assert shed.status_code == 503 and shed.headers["retry-after"] == "5" and shed.json()["available_mb"] == 100
    assert client.get("/api/rag/health").status_code == 200
    monkeypatch.setattr(os_limits, "memory_status", lambda: {"available_mb": 500})
    assert client.post("/api/rag/retrieve").status_code == 200

def test_health_reports_memory(client):
    body = client.get("/health").json()
    assert "available_mb" in body["memory"] and "min_available_mb" in body["os_limits"]
