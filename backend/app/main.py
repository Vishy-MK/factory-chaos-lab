import time
from typing import Any

# Thread and CPU limits must be injected before numpy/torch/faiss are imported.
from app.os_limits import apply_os_limits, memory_status

OS_LIMITS = apply_os_limits()

import numpy as np
import psutil
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.middleware import InferenceTimingMiddleware, MemoryGuardMiddleware

START_TIME = time.time()

app = FastAPI(title="Bosch Inference Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Inference-Time-Ms"],
)
app.add_middleware(InferenceTimingMiddleware)
app.add_middleware(MemoryGuardMiddleware, min_available_mb=OS_LIMITS["min_available_mb"])


class PredictRequest(BaseModel):
    data: list[float] = Field(default_factory=list, description="Flattened input tensor")
    shape: list[int] | None = Field(None, description="Optional tensor shape, e.g. [1,3,640,640]")
    metadata: dict[str, Any] = Field(default_factory=dict)


@app.get("/health")
def health() -> dict:
    mem = psutil.virtual_memory()
    return {
        "status": "ok",
        "server_uptime_s": round(time.time() - START_TIME, 2),
        "system_uptime_s": round(time.time() - psutil.boot_time(), 2),
        "ram": {
            "total_mb": round(mem.total / 2**20, 1),
            "used_mb": round(mem.used / 2**20, 1),
            "percent": mem.percent,
        },
        "memory": memory_status(),
        "os_limits": OS_LIMITS,
    }


@app.post("/api/v1/predict")
def predict(req: PredictRequest) -> dict:
    t0 = time.perf_counter()
    x = np.asarray(req.data, dtype=np.float32)
    if req.shape:
        x = x.reshape(req.shape)
    detections = [{"class_id": 0, "label": "object", "confidence": 0.92, "bbox": [0, 0, 100, 100]}]
    return {
        "input_shape": list(x.shape),
        "input_mean": float(x.mean()) if x.size else None,
        "detections": detections,
        "metadata": req.metadata,
        "inference_time_ms": round((time.perf_counter() - t0) * 1000, 3),
    }


# Register RAG on the existing application, preserving routes and middleware.
from app.rag.api import register_rag

register_rag(app)
