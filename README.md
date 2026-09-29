# GrantAnchor

Most grant money comes with rules. Travel is capped at $6,000. Equipment over $5,000 needs the funder's sign-off. Contractors outside the country need approval. Nothing can be spent after the end date.

Nobody breaks these on purpose. The problem is memory. The rules were signed in January, three different people spend money over the next year, and by September nobody remembers that travel was capped across the whole team. The funder notices at audit time and asks for the money back.

GrantAnchor checks every expense against the grant's rules and against everything the team has already spent, before the money goes out. And it learns: every stopped payment, funder approval and invoice that came in higher than expected goes into [Hindsight](https://hindsight.vectorize.io) memory, so month nine's answers are sharper than day one's.

![Memory changing the answer](docs/screenshots/demo_desktop_2_memory_changed.png)

## Where memory makes the difference

| Rules alone | With Hindsight memory |
|---|---|
| "$600 flight for Maya. Fits the travel limit. Good to go." | "Maya's last two final invoices came in about 50% higher. This will really be about $902, which takes travel to 84% of the $2,500 limit. Be careful." |
| "Studio Nord is a foreign contractor. Blocked." | "Studio Nord was paid under funder approval HLRF-PA-12 in April. **Use HLRF-PA-12**?" One click. |
| The same answer on day 1 and day 270. | A learning curve on the Overview, and lessons Hindsight writes with `reflect`. |

Memory can make an answer more cautious and can hand you what you need, like an approval reference. It can never block or approve on its own; the grant's rules decide. The full explanation of what is stored, how it's recalled and how it stays correct when Hindsight is down is in **[docs/HINDSIGHT.md](docs/HINDSIGHT.md)**.

## What it does

- **Reads your grant.** Upload the award letter (PDF, Word or text). It pulls out the amount, the dates and the spending rules, and shows them to you before saving anything. Every rule can be edited later.
- **Answers "can we spend this?" while you type.** You get one of three answers:
  - **Good to go**
  - **Okay, but be careful** (a limit is getting close)
  - **Don't spend this yet** (a rule would be broken)

  Each comes with the reason in plain words and a 0 to 100 risk score.
- **Remembers.** Every recorded expense is kept, so a $1,000 flight gets flagged when someone else already spent $2,000 on travel this quarter. When history changes the answer, it shows you which past expenses did it.
- **Gets better the more you use it.** Stopped payments, funder approvals and overruns are saved to Hindsight and change the next answer (see the table above). The Memory tab lists everything it has learned.
- **Handles other currencies.** Pay a vendor in euros or rupees and it's converted to the grant's currency at the European Central Bank rate for that day. You can use your own rate instead. Both amounts are kept.
- **Keeps a clean record.** Expenses can be edited or removed. Every change needs a reason and is kept in History. An edit that would break a rule needs your confirmation.
- **Gives a second opinion.** An AI model reads the clauses that can't be checked by rules, like reporting deadlines, and adds notes. It can never change the answer.

## Try it

```bash
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --port 8000
```

Open http://localhost:8000, create an account, upload a grant agreement (try `examples/sample_award_letter.docx`), and start checking expenses.

**See it nine months in.** The demo workspace is a four-person startup halfway through a made-up $150,000 grant: 31 expenses and 7 remembered experiences. With `HINDSIGHT_API_KEY` set, all of it syncs to Hindsight on startup.

```powershell
$env:SEED_PATH="examples/demo_workspace.json"; $env:DATA_DIR="data/demo"; uvicorn app.main:app --port 8000
```

```bash
SEED_PATH=examples/demo_workspace.json DATA_DIR=data/demo uvicorn app.main:app --port 8000
```

Then follow [examples/DEMO_SCRIPT.md](examples/DEMO_SCRIPT.md). A command-line version:

```bash
python run_demo.py
```

Both API keys in `.env` are optional:

- Without `GROQ_API_KEY`, the file import only picks up simple caps and foreign-contractor rules, and there's no AI second opinion.
- Without `HINDSIGHT_API_KEY`, memory stays in the local database.

## How it decides

The answer comes from plain code, not from the AI. For each expense it:

1. Works out the category (or uses the one you picked) and converts the amount to the grant's currency.
2. Reads the country from "City, Country". If it can't tell (is "CA" Canada or California?), it asks you instead of guessing.
3. Runs every rule on the grant: spending caps, per-person caps, per-vendor caps, country restrictions, approval thresholds, blocked categories and vendors, allowed categories, and the grant dates.
4. Caps can apply over the whole grant, over any rolling number of days, or per fiscal month, quarter or year. A backdated expense is checked against every window it falls into.
5. Exactly at the cap is allowed. One cent over is not. Past the warning level (80% by default) you get the amber answer.

The AI only sees the result afterwards. Its notes are labelled as advisory.

## Where your data lives

| What | Where |
|---|---|
| Grants, rules, team, expenses, history, accounts | SQLite file at `data/grantanchor.sqlite3` |
| A searchable copy of every expense | Your [Hindsight](https://hindsight.vectorize.io) memory bank, if configured |
| Exchange rates | Cached in the same SQLite file after the first lookup |

The local database does all the arithmetic. Hindsight is used for the "Ask memory" search and as context for the AI second opinion. Ask memory only shows expenses that are still in your ledger, so deleted or old entries never come back.

Passwords are stored as salted hashes. Sessions are an httpOnly cookie.

## Settings

| Variable | Default | What it's for |
|---|---|---|
| `GROQ_API_KEY` | empty | Turns on file import by AI and the second opinion |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Which Groq model to use |
| `HINDSIGHT_API_KEY` | empty | Turns on Hindsight memory |
| `HINDSIGHT_BANK_ID` | `your-bank-id` | Memory bank name. Created on first use |
| `HINDSIGHT_BASE_URL` | `https://api.hindsight.vectorize.io` | |
| `REQUIRE_LOGIN` | `true` | Set to `false` for single-person local use |
| `COOKIE_SECURE` | `false` | Set to `true` when served over HTTPS |
| `DATA_DIR` | `./data` | Where the database lives |
| `FX_BASE_URL` | `https://api.frankfurter.dev/v1` | Exchange-rate source |
| `SEED_PATH` | empty | JSON file to pre-fill a new workspace. Used by tests |

## API

The full, clickable reference is at http://localhost:8000/docs once the server runs. Everything under `/api` needs a signed-in session except `/api/auth/*` and `/api/health`.

The main calls:

| Call | What it does |
|---|---|
| `POST /api/expenses/preview` | Instant check, with and without history. Saves nothing |
| `POST /api/audit-expense` | Check and record the expense if nothing blocks it |
| `POST /api/expenses/audit/compare` | Same as preview, plus Hindsight recall and the AI second opinion |
| `POST /api/grants/extract-file?filename=` | Read a grant file sent as the raw request body |
| `POST /api/grants`, `PATCH /api/grants/{id}` | Create or edit a grant and its rules |
| `GET /api/grants/{id}/dashboard` | Burn rate, runway, limit usage, monthly spending |
| `PATCH /api/memories/{id}` | Correct an expense. Answers 409 if the edit breaks a rule, unless `confirm` is true |
| `GET /api/memory/recall?grant_id=&q=` | Ask memory |
| `GET /api/ledger.csv?grant_id=` | Export |

The original prototype endpoints still work: `/api/state`, `/api/switch-grant`, `/api/audit-expense` and `/api/reset-seed`.
## Who it's for, and how it gets adopted

- **Who:** startups, university labs and nonprofits running one to a few grants (SBIR/STTR, NSF, foundation awards) without a grants office. The person using it is the founder or ops lead who approves spending.
- **How it starts:** upload the award letter, check the next expense. No integration needed on day one.
- **What comes next:**
  - a Slack or email "check before you pay" command
  - import from Brex or Ramp card feeds, so expenses arrive automatically
  - an export formatted for the funder's financial report

## Tests

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium
python -m pytest
ruff check app tests scripts run_demo.py
```

102 tests. They cover:
- every rule type and its edge cases (exactly at the cap, rolling-window boundaries, backdated entries, ambiguous locations)
- the Hindsight and Groq clients (timeouts, 429s, bad JSON)
- memory findings and the outbox
- currency conversion
- uploads and edits
- five browser runs on desktop and mobile, including the demo workspace

## Docs

- [docs/HINDSIGHT.md](docs/HINDSIGHT.md): how memory is used
- [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md): every module and function, and where you could reuse it
- [examples/DEMO_SCRIPT.md](examples/DEMO_SCRIPT.md): a walk-through with verified answers
- [docs/launch/](docs/launch/): demo video script, LinkedIn and Reddit posts

## Known limits

- **One shared workspace.** Everyone who signs in sees every grant. Roles are recorded but don't restrict anything yet.
- **Login is not rate-limited.** Put it behind a proxy that does that before exposing it to the internet.
- **Scanned PDFs can't be read.** Run OCR first, or paste the text.
- **About 30 currencies are covered,** the ones the ECB publishes. For anything else, enter your own rate.
- **A grant's currency is locked** once it has expenses.
- **The AI import can miss or misread a clause.** That's why you review the rules before saving.

## Project layout

```
app/        backend (FastAPI): rules engine, memory (learning.py, memory_sync.py, hindsight.py), store, FX, import, auth
static/     the web app (index.html) and sign-in page
examples/   sample award letter, demo workspace, demo script
docs/       Hindsight explanation, architecture guide, screenshots, launch posts
scripts/    maintenance scripts
tests/      pytest suite (unit, API, browser)
run_demo.py standalone CLI simulation
```

MIT licence.
