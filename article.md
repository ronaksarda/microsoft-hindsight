# Why Our Compliance Engine Pairs SQLite Rules With Hindsight Memory

Last year, a research lab I know had to wire back $38,000 to a federal agency because three different researchers booked flights that, combined, exceeded their grant’s annual travel ceiling by 12%. Nobody stole anything; nobody misclassified an expense on purpose. The agreement had been signed eleven months earlier, sat in a folder nobody opened, and the team’s accounting software only looked at account balances, not contractual clauses.

When I set out to build GrantAnchor, my first naive instinct was what many engineers try today: chuck the contract PDF into an LLM context window or a standard vector database, pass every incoming receipt through an agent prompt, and ask "is this allowed?" 

That approach failed almost immediately. 

LLMs hallucinate currency exchange math, stumble over rolling calendar windows, and fail unpredictably when evaluating whether an expense falls inside a fiscal quarter. But the inverse approach—relying entirely on hardcoded SQL constraints—failed too. Real grant compliance isn't just scalar math. It depends on institutional precedent: Which prior agency approval reference did we use for this German contractor six months ago? Which team members consistently run 40% over their initial hardware estimates? What unwritten operational rules did the program manager establish in an email chain?

To solve this, I split the system into two distinct layers: a deterministic policy engine in SQLite that strictly controls the money, and an episodic agent memory layer powered by [Hindsight](https://github.com/vectorize-io/hindsight).

Here is how the architecture hangs together, what broke along the way, and what I learned about building production-grade agent memory.

---

## What the System Does and How It Hangs Together

GrantAnchor acts as an automated pre-commitment gatekeeper. Before anyone swipes a card or commits to a purchase order, the expense runs through an audit pipeline that returns one of three verdicts:
1. **APPROVED**: Fully within budget and policy limits.
2. **APPROVED_WITH_WARNINGS**: Allowable, but approaching a rolling cap threshold (e.g., 80% of travel spend reached).
3. **CLAWBACK_RISK_DETECTED**: Blocked immediately with the exact contract clause violated and the math behind it.

The system ingests the raw grant agreement (PDF, DOCX, or text), extracts structured policy constraints (fiscal caps, rolling-window limits, foreign contractor restrictions, and prior-approval thresholds), and enforces them on every transaction.

```
                           +---------------------------+
                           |   Incoming Expense Event  |
                           | (Amount, Vendor, Spender) |
                           +-------------+-------------+
                                         |
                                         v
                     +---------------------------------------+
                     |    Deterministic Rule Engine (Core)   |
                     | - ECB Currency Normalization (EUR/USD)|
                     | - ISO Country Resolution              |
                     | - Rolling Window & Cap Math (SQLite)  |
                     +-------------------+-------------------+
                                         |
                       +-----------------+-----------------+
                       |                                   |
                [Hard Block/Pass]                  [Semantic Context]
                       |                                   |
                       v                                   v
             +--------------------+            +-----------------------+
             | SQLite Ledger / DB |            |  Hindsight Memory API |
             | (System of Record) |<---Outbox--|  (Precedent & Drift)  |
             +--------------------+   Worker   +-----------------------+
                                                           |
                                                           v
                                               +-----------------------+
                                               |  Advisory Briefing &  |
                                               |    Meta-Reflections   |
                                               +-----------------------+
```

The key architectural decision is that **the LLM never decides an audit verdict**. The engine evaluates deterministic rules directly. But alongside the math, the system queries long-term semantic memory to surface vital operational context: prior approval waivers, historical vendor approvals, and personal budget drift.

---

## The Core Technical Challenge: Ephemeral Context vs. Episodic Memory

If you've ever tried building persistent agents with raw vector search (traditional RAG), you've likely hit the standard wall: similarity search on chunked embeddings is great at finding text documents, but terrible at acting as [agent memory](https://vectorize.io/what-is-agent-memory).

When an engineer submits an expense for `"Studio Nord GmbH"` under category `"Contractor"`, naive vector search retrieves random invoice chunks containing the word "Studio" or "Contractor." It doesn't understand:
- That this specific entity is an overseas vendor requiring a 30-day agency waiver.
- That three months ago, our operations lead submitted waiver letter `HLRF-PA-12` which was approved by the funder.
- That this approval can be legally cited on future invoices for the same project scope.

Instead of writing a custom graph database or bolting together ad-hoc embedding pipelines, I integrated [Vectorize Hindsight](https://hindsight.vectorize.io/), an open-source memory engine designed specifically for autonomous systems.

In GrantAnchor, Hindsight functions as an episodic memory bank. Every recorded transaction, administrative override, stopped payment, and post-facto invoice correction is retained into Hindsight with structured semantic tags (`grant:<id>`, `kind:<type>`, `vendor:<slug>`, `spender:<id>`). When evaluating a new transaction, GrantAnchor recalls related operational memories before the user commits funds.

---

## Code-Backed Implementation: How We Wired It

### 1. Resilient Outbox and Circuit Breaking

In a financial system, your semantic memory service must never be a single point of failure. If the memory API has high latency or network partitions occur, the primary spending ledger must remain fast and operational. 

We implemented a per-bank circuit breaker and an asynchronous outbox in [`app/hindsight.py`](app/hindsight.py):

```python
class HindsightClient:
    """Thin, schema-validated Hindsight REST client with a per-bank circuit breaker."""

    def __init__(self, *, base_url: str | None = None, api_key: str | None = None,
                 bank_id: str | None = None, timeout: float | None = None) -> None:
        self.base_url = (base_url or settings.hindsight_base_url).rstrip("/")
        self.api_key = api_key or settings.hindsight_api_key
        self.bank_id = bank_id or settings.hindsight_bank_id
        self.timeout = timeout or settings.hindsight_timeout_seconds
        self._cooldown_until = 0.0

    @property
    def circuit_open(self) -> bool:
        return time.monotonic() < self._cooldown_until

    def trip(self, seconds: float) -> None:
        self._cooldown_until = time.monotonic() + seconds

    async def _request(self, method: str, path: str, json: Any = None, 
                       *, trip_on_fail: bool = True) -> httpx.Response:
        if not self.api_key:
            raise HindsightDisabled()
        if self.circuit_open:
            raise HindsightError("circuit open", retryable=True)
            
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
                resp = await client.request(method, path, json=json, headers=self._headers())
        except httpx.HTTPError as exc:
            if trip_on_fail:
                self.trip(30.0)
            raise HindsightError(f"transport error: {exc}") from exc

        if resp.status_code >= 400:
            retryable = resp.status_code in (408, 409, 425, 429) or resp.status_code >= 500
            if trip_on_fail and retryable:
                self.trip(30.0)
            raise HindsightError(f"HTTP {resp.status_code}", status_code=resp.status_code, retryable=retryable)
        return resp
```

When an expense is recorded, it writes to local SQLite first. If Hindsight is reachable, it retains asynchronously; if the circuit trips or an error occurs, the record is flagged in the local outbox for background synchronization.

### 2. Retaining Experiential Precedent

We don't just store receipts; we store *experiences*. When an expense is stopped by a policy rule, or when an administrative user attaches an approval code, we capture that event in [`app/learning.py`](app/learning.py):

```python
async def remember(kind: Kind, grant_id: str, text: str, *, 
                   vendor: str = "", spender_id: str = "", spender: str = "", **meta: Any) -> dict[str, Any]:
    """Save an experience locally and retain it in Hindsight without blocking callers."""
    event = {
        "id": f"evt_{uuid.uuid4().hex[:10]}",
        "grant_id": grant_id,
        "kind": kind,  # 'decision', 'approval', or 'overrun'
        "text": text,
        "vendor": vendor,
        "vendor_key": normalize_vendor(vendor),
        "spender_id": spender_id,
        "created_at": clock.now_iso(),
    }
    
    client = hindsight.get_client()
    if client.enabled and not client.circuit_open:
        item = hindsight.MemoryItem(
            content=text,
            timestamp=event["created_at"],
            context=f"grant-experience:{grant_id}",
            metadata={k: str(v) for k, v in {**meta, "kind": kind, "event_id": event["id"], 
                                              "vendor": vendor, "spender": spender}.items() if v is not None},
            document_id=event["id"],
            tags=["grantanchor", f"grant:{grant_id}", f"kind:{kind}", f"vendor:{normalize_vendor(vendor)}"],
        )
        try:
            await client.retain([item], async_=True)
            event["sync_status"] = "synced"
        except hindsight.HindsightError as exc:
            event["sync_status"] = "failed"
            logger.warning("Retain failed: %s", exc)
            
    local_store.conn().execute(
        "INSERT INTO memory_events(id, grant_id, kind, created_at, data) VALUES(?, ?, ?, ?, ?)",
        (event["id"], grant_id, kind, event["created_at"], json.dumps(event)),
    )
    return event
```

Using deterministic `document_id` values ensures that re-running sync from the outbox is strictly idempotent. Hindsight updates or appends without creating duplicate vector noise.

### 3. Contextual Recall and Briefing During Evaluation

When a user types an expense, the UI calls `/api/expenses/preview` which triggers `briefing()`. This pulls both semantic memories from Hindsight and exact historical metrics from the local database:

```python
async def briefing(grant_id: str, *, vendor: str = "", spender_id: str = "", 
                   spender: str = "", category: str = "") -> dict[str, Any]:
    """Retrieve institutional memory about this vendor/person/category."""
    client = hindsight.get_client()
    recalled: list[dict[str, Any]] = []
    
    if client.enabled and not client.circuit_open and (vendor or spender or category):
        query = f"{vendor} {spender} {category} approvals overruns stopped payments".strip()
        try:
            # Query Hindsight semantic recall scoped to this grant's bank
            results = await client.recall(query, tags=[f"grant:{grant_id}"])
            for r in results:
                recalled.append({"text": r.text, "metadata": r.metadata})
        except hindsight.HindsightError as exc:
            logger.info("Recall fallback to local events: %s", exc)
            
    # Calculate deterministic overruns and past approval suggestions
    local_events = events(grant_id)
    hints = _hints(local_events, vendor, spender_id)
    return {"source": "hindsight" if recalled else "local", "memories": recalled[:5], **hints}
```

### 4. Synthesizing High-Level Lessons via Reflection

Rather than running expensive batch map-reduce jobs to understand compliance trends, GrantAnchor leverages Hindsight's `reflect` capability. Hindsight analyzes the bank's accumulated memories against a structured JSON schema:

```python
async def lessons(grant_id: str, grant_name: str) -> dict[str, Any]:
    """Use Hindsight reflect to extract high-level operational lessons."""
    client = hindsight.get_client()
    prompt = (
        f"What has this team learned so far about spending on the grant '{grant_name}'? "
        "Give at most 4 short, practical lessons for the next person about to spend money: "
        "limits that keep getting close, people whose final invoices run over, and vendors that "
        "needed prior funder approval. Only use facts in memory."
    )
    
    response = await client.reflect(
        prompt,
        tags=[f"grant:{grant_id}"],
        response_schema=LESSONS_SCHEMA,
    )
    return {"lessons": response.structured_output.get("lessons", [])}
```

This generates tangible, human-readable insights directly on the dashboard: *"Contractor Studio Nord in Germany requires agency prior approval HLRF-PA-12; attach this ref before submission to prevent hold."*

---

## Real-World Behavior: Two Concrete Scenarios

Here is how this dual architecture functions in practice compared to standard tooling.

### Scenario A: The Repeat Foreign Contractor

- **The Setup**: Grant clause 9.1 forbids payments to contractors outside the home country (United States) without 30 days prior written agency approval.
- **The Event**: In March, an engineer hired *Studio Nord GmbH* (Berlin) for a specialized CFD audit. The initial check was blocked. Operations obtained approval letter `HLRF-PA-12` from the program officer, entered the approval reference, and cleared the payment. GrantAnchor saved this event to Hindsight.
- **The Follow-Up**: In October, a different engineer attempts to book *Studio Nord* for $4,500.
- **System Behavior**:
  - The deterministic engine notes the foreign jurisdiction and flags that a prior approval reference is mandatory.
  - Simultaneously, Hindsight's recall matches the vendor name and surfaces:
    > *"Last time Studio Nord was paid under funder approval HLRF-PA-12. Click to reuse."*
  - The engineer clicks once, attaching the approved reference. The audit passes in milliseconds. No back-and-forth emails, no audit violation.

### Scenario B: Catching Systematic Invoice Drift

- **The Setup**: A team member regularly estimates travel or lab supplies optimistically. On the purchase request, they quote $1,800.
- **The Reality**: Their actual final invoices historically average 68% higher than initial authorizations.
- **System Behavior**:
  - The travel budget only has $2,200 remaining before hitting the hard cap.
  - While $1,800 is mathematically under $2,200, Hindsight recalls three past upward invoice adjustments for this team member.
  - The system warns:
    > *"Warning: Maya's expenses historically overrun initial estimates by an average of 68.2%. At that rate ($3,027.60), this expense will breach the Clause 4.2 Travel Cap by $827.60."*
  - The lead researcher pauses the purchase before committing the funds, avoiding an unallowable overrun.

---

## Lessons Learned

Building this system reinforced several lessons about agent architecture that go against common industry narratives:

1. **Keep the financial ledger boring and deterministic.**
   Never let an LLM or probabilistic model decide whether a number is greater than another number. Use SQLite, PostgreSQL, or standard Python math for rules and limits. Use agent memory to enrich the decision with context, precedent, and heuristics.

2. **Tags and metadata are not optional in agent memory.**
   Pure cosine similarity across raw text strings degrades quickly as your data grows. In Hindsight, scoping recalls with structured tags (`grant:NSF-2026-881`, `kind:approval`) made retrieval fast, deterministic, and isolated between different grants.

3. **Design for memory service degradation from day one.**
   Network requests fail. Third-party APIs experience downtime. Putting Hindsight behind a circuit breaker and an asynchronous outbox ensured our web app never hung on an expense check even if the memory service was temporarily unreachable.

4. **Reflection is fundamentally different from retrieval.**
   Standard RAG answers "find me something similar to X." Reflection answers "what patterns have emerged across all our past actions?" Offloading that synthesis to Hindsight's `reflect` API eliminated thousands of lines of messy custom aggregation logic.

---

## Conclusion

The next generation of enterprise software won't be chatbots that replace entire workflows, nor will it be legacy forms that leave humans to remember hundreds of pages of contractual fine print.

By anchoring deterministic rules in local code and offloading long-term operational memory to [Hindsight](https://github.com/vectorize-io/hindsight), we built a compliance system that is mathematically airtight while continuously learning from experience.

If you are building autonomous agents or decision-support tools that need to remember past interactions without hallucinating the fundamentals, check out the [Hindsight documentation](https://hindsight.vectorize.io/) and experiment with purpose-built [agent memory](https://vectorize.io/what-is-agent-memory).
