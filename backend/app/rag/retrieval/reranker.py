from threading import RLock
from app.rag.errors import RAGError

class Reranker:
    def __init__(self, model):
        self.model_name, self.model = model, None
        self.lock = RLock()

    def rerank(self, query, hits):
        if not hits:
            return hits
        try:
            with self.lock:
                if self.model is None:
                    from sentence_transformers import CrossEncoder
                    self.model = CrossEncoder(self.model_name, device="cpu", trust_remote_code=False)
                scores = self.model.predict([(query, h.text) for h in hits])
            # Keep original cosine scores; reranking logits are not confidence.
            ordered = [hit for _,hit in sorted(zip(scores, hits), key=lambda item: float(item[0]), reverse=True)]
            return [h.model_copy(update={"rank": i+1}) for i,h in enumerate(ordered)]
        except Exception as exc:
            raise RAGError("Reranking failed; check model availability") from exc
