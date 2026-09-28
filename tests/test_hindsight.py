import pytest
import time
from app import hindsight
from app.config import settings

@pytest.mark.anyio
async def test_hindsight_circuit_breaker_fast_fallback():
    bank_id = "test-nonexistent-bank-12345"
    hindsight.clear_bank(bank_id)

    # First call might check or hit timeout/404, but subsequent calls must be instantaneous (< 0.1s)
    t0 = time.time()
    res1 = await hindsight.retain(bank_id, "[GRANT RULE] Clause 9.1: Foreign contractor ban", context="rule")
    first_duration = time.time() - t0

    t1 = time.time()
    for i in range(5):
        await hindsight.retain(bank_id, f"[EXPENSE] Test spend {i}", context="expense")
    batch_duration = time.time() - t1

    # 5 calls must complete in under 0.5s total with circuit breaker active
    assert batch_duration < 0.5, f"Batch calls took {batch_duration}s, circuit breaker failed to prevent slow retries!"

    # Recall should also be fast
    t2 = time.time()
    recalled = await hindsight.recall(bank_id, "foreign contractor", top_k=2)
    recall_duration = time.time() - t2
    assert recall_duration < 0.2, f"Recall took {recall_duration}s"
    assert len(recalled) > 0
    assert "Clause 9.1" in recalled[0]["content"]
