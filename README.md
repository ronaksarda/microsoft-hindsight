# GrantAnchor

**Autonomous grant compliance and budget milestone memory engine.**

Startups, non-profits, and research labs lose grant awards and face clawbacks when everyday operational spending quietly violates grant agreements signed months earlier. GrantAnchor uses [Vectorize Hindsight](https://hindsight.vectorize.io) persistent memory to track grant stipulations, cumulative spending buckets, and milestone deadlines over time — proactively intercepting non-compliant expenditures before they trigger agency action.

---

## Architecture

```
┌──────────────┐     ┌───────────────────┐     ┌─────────────────┐
│   Dashboard  │────▶│  FastAPI Server    │────▶│  Hindsight API  │
│  (Tailwind)  │     │  /api/seed         │     │  retain / recall│
│              │◀────│  /api/audit-expense│◀────│                 │
└──────────────┘     │  /api/retain-rule  │     └─────────────────┘
                     │  /api/memories     │              │
                     └────────┬──────────┘              │
                              │                         │
                     ┌────────▼──────────┐     ┌────────▼────────┐
                     │  Compliance Engine │────▶│   Groq LLM      │
                     │  (audit logic)     │     │   (llama-3.3)   │
                     └───────────────────┘     └─────────────────┘
```

## Quick Start

```bash
# 1. Clone and install
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env with your GROQ_API_KEY and HINDSIGHT_API_KEY

# 3. Run the server
uvicorn app.main:app --port 8000

# 4. Open dashboard
# http://localhost:8000
```

## CLI Demo

```bash
python run_demo.py
```

Seeds the NSF-2026-881 grant lifecycle into Hindsight, audits a foreign contractor invoice, and asserts that Clause 9.1 is flagged as a critical clawback risk.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/seed` | Load seed grant lifecycle into Hindsight |
| `POST` | `/api/retain-rule` | Store a grant rule or milestone condition |
| `POST` | `/api/audit-expense` | Audit expense against Hindsight memory |
| `GET`  | `/api/memories` | List all stored grant terms and events |
| `GET`  | `/` | Serve the dashboard |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `GROQ_API_KEY` | Groq API key for LLM compliance analysis |
| `HINDSIGHT_API_KEY` | Vectorize Hindsight API key |
| `HINDSIGHT_BASE_URL` | Hindsight endpoint (default: cloud) |
| `HINDSIGHT_BANK_ID` | Memory bank identifier |
| `PORT` | Server port (default: 8000) |

Both API keys are optional for local development — the system falls back to in-memory storage and deterministic compliance analysis.

## Seed Data Scenario

**Grant:** National Science Tech Grant #NSF-2026-881 ($250,000)

| Month | Event | Details |
|-------|-------|---------|
| 1 (Jan) | Award Baseline | 4 grant rules retained: budget cap, contractor cap ($40k), foreign contractor restriction (Clause 9.1), travel cap ($8k) |
| 3 (Mar) | Logged Spend | $5,200 domestic travel for AI conference |
| 6 (Jun) | Test Expense | $8,500 to Nordic Tech Solutions (Oslo, Norway) for Milestone 2 UI/UX work |

**Expected Result:** The Month 6 expense triggers `VIOLATION_DETECTED` with `CRITICAL_CLAWBACK_RISK` because Clause 9.1 strictly prohibits foreign contractors without prior written approval.

## Stack

- **Python 3.10+** with FastAPI + Uvicorn
- **Vectorize Hindsight** for persistent compliance memory
- **Groq** (llama-3.3-70b-versatile) for structured compliance analysis
- **Tailwind CSS** (CDN) for the dashboard

## License

MIT
