# LeadLoop

An AI agent that runs a small data business end to end. SerpApi powers both halves:
it **finds buyers** (people publicly asking for data) and **builds what they buy**
(custom business datasets). A human can approve, edit or reject any step.

SerpApi India Hackathon 2026 — track: AI Agents.

## Pipeline

```
discover -> qualify -> build sample -> draft pitch -> [you approve] -> send
                                                                         |
          +-------------------- wait for the lead <----------------------+
          |  reply                         | silence (follow-up due)     ^
          v                                v                             |
     read + price (rules)            draft follow-up                     |
          |                                |                             |
          +-------> [you approve] -> send reply -> won / lost / wait ----+
```

| Stage | What happens | SerpApi engine |
|---|---|---|
| Discover | Companies hiring for research / lead-gen / data entry, Reddit posts asking for lists, freshly funded startups. Signals for the same company are merged. | `google_jobs`, `google`, `google_news` |
| Score | Deterministic intent score; every point has a reason | — |
| Qualify | The LLM (Groq, Gemini or Claude) reads the evidence and picks the dataset that would help most | — |
| Sample | A real 10-row dataset for that lead, built *before* pitching | `google_maps` |
| Pitch | Short email citing their exact need, sample attached as CSV | — |
| Negotiate | The LLM classifies each reply (interested, question, price objection, accept, not now, unsubscribe). **Code sets every price** from your pricing rules; the LLM only words it | — |
| Follow up | Drafted automatically after `FOLLOWUP_AFTER_DAYS` of silence, up to `MAX_FOLLOWUPS` | — |
| Gates | LangGraph `interrupt()` pauses before every outgoing email until you approve, edit or discard it. Autopilot can skip the pitch gate, the reply gate, or both | — |

### LLM providers

Set any of `GROQ_API_KEY` (free: 1,000 requests/day per model at console.groq.com),
`GEMINI_API_KEY` or `ANTHROPIC_API_KEY`. They're tried in that order, each with a backup model;
if all fail (quota, outage) the agent falls back to rules and the live panel says so.
Groq uses strict structured output, so every answer matches its Pydantic schema.

### Pricing rules

Set in `.env`. Defaults: ₹4 per row, ₹1,500 minimum, 500 rows if the lead doesn't say.
Each price objection takes `DISCOUNT_STEP_PCT` off, down to `MAX_DISCOUNT_PCT`. After that the
agent offers a smaller scope at the same rate instead of going lower. Accepting never re-prices.

## Board and Insights

`http://localhost:8000` has two views:

- **Find leads** opens a live panel showing the agent's work as it happens (streamed from
  `GET /api/discover/stream`): the search plan, each SerpApi search with its engine, query, result
  count, timing and whether it was live (1 credit) or cached (free), how signals were merged, and
  every lead kept or dropped with the reason.
- **Board**: deals as cards in five columns (Discovered, Needs your approval, Waiting on lead,
  Won, Closed out). Click a card for the evidence, sample, conversation, quote and audit trail.
- **Insights**: KPI tiles, a live agent-flow diagram (where every deal is, which steps need you),
  sales funnel, lead sources, negotiation chart (quoted price per round), intent-score
  histogram, who did the work (agent / you / leads), and recent activity. All numbers come from
  `GET /api/stats`; every chart has hover details and a table view.

## Run

```bash
docker compose up -d                      # Mailpit inbox at http://localhost:8025
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                      # add keys, or leave empty for DEMO mode
.venv/bin/uvicorn app.main:app --reload   # board: http://localhost:8000  ·  API docs: /docs
.venv/bin/python -m pytest -q
```

Without keys everything runs on `backend/fixtures/` (fictional demo data) and template text.
With keys, every live SerpApi response is cached in `backend/data/serp_cache/` and a ledger
stops live calls at `LEADLOOP_CREDIT_BUDGET`.

## Sending real email

Any SMTP account works. The setup we use (free): **Brevo** sends, replies land in the Zoho
inbox of the From address, and you paste them onto the board.

1. Create a free Brevo account. Under **Senders, Domains & Dedicated IPs → Domains**, add
   `hr-sita.in` and add the DNS records it shows (Brevo code, DKIM, DMARC) at your domain registrar.
2. Under **SMTP & API → SMTP**, generate an SMTP key. Note the **Login** shown there.
3. In `backend/.env`: `SMTP_USER=<that login>`, `SMTP_PASSWORD=<SMTP key>`, `LEADLOOP_SEND_MODE=live`.
4. Restart, then open `/api/mail/check`. It logs in without sending anything.
5. Every deal now needs the lead's email. When they reply, paste it into the deal on the board.

Rehearse with your own second address as the "lead" first.

With a paid Zoho plan (IMAP enabled), set `IMAP_HOST=imappro.zoho.in` plus `IMAP_USER` /
`IMAP_PASSWORD`, and replies are read automatically every minute instead.

## API

| Method | Path | |
|---|---|---|
| GET | `/api/health` | modes, credits, sends today, pricing |
| GET | `/api/mail/check` | test SMTP (and IMAP, if set) login |
| POST | `/api/discover` | find and score leads |
| GET | `/api/leads` | ranked leads with evidence |
| POST | `/api/leads/{id}/deal` | `{"email"?, "autopilot"?: ["pitch", "reply"]}` |
| GET | `/api/deals`, `/api/deals/{id}` | state, conversation, quote, audit trail, `pending_gate` |
| POST | `/api/deals/{id}/decision` | `{"action": "approve" \| "edit" \| "reject", "draft"?, "note"?}` |
| POST | `/api/deals/{id}/simulate-reply` | record the lead's reply: role-play in sandbox, or paste it when there's no IMAP |
| POST | `/api/deals/{id}/nudge` | draft the next follow-up now |
| POST | `/api/inbox/poll` | read replies now (IMAP only) |
| GET | `/api/deals/{id}/sample.csv` | the sample dataset |

## Demo video

`demo/make_demo.sh` records the demo end to end: macOS text-to-speech narration
(`demo/narration.json`), a scripted Playwright run through every feature on a sandboxed copy of
the app (port 8002, mail to Mailpit, your data untouched), then ffmpeg editing that shortens idle
AI waits and lays each narration line at the moment its step appears. Output: `demo/leadloop-demo.mp4`
plus `.srt` subtitles.

## Guardrails

- Sandbox mode (default) sends everything to Mailpit. Live mode has a daily send cap, an opt-out
  line on every email, and a suppression list: anyone who replies "stop" is never emailed again.
- The LLM is told exactly what the product is and may not claim more (no "hand-verified", no
  emails or owner names, no prices it wasn't given).
- No social-network logins or scraping: discovery uses public search results via SerpApi only.
- Every agent and human action is written to the deal's audit trail.
