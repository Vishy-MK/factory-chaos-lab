def recall_at_k(retrieved_ids: list[str], relevant_ids: set[str], k: int) -> float:
    if k < 1:
        raise ValueError("k must be positive")
    return len(set(retrieved_ids[:k]) & relevant_ids) / len(relevant_ids) if relevant_ids else 0.0

def reciprocal_rank(retrieved_ids: list[str], relevant_ids: set[str]) -> float:
    return next((1.0 / rank for rank, identifier in enumerate(retrieved_ids, 1) if identifier in relevant_ids), 0.0)
