# Cap-resilient research corroboration (Step 4)

Status: **proposed** (2026-09-20). Design doc for review — no code yet.

## Background

Corroboration and several analysis tiers go dark whenever the single Anthropic
account hits its usage/spend cap (observed 2026-09-19/20; regain 2026-10-01).
Separately, most Financials-tab figures never earn VERIFIED because independent
corroboration is narrow, and figures that are internally impossible (EBIT > gross
profit, a balance-sheet total in thousands beside a billions revenue) were shown
as confident clean numbers.

The **deterministic** side of that gap is already addressed and shipping:

- **Consistency verifier** — read-time accounting-identity / ordering /
  magnitude checks that badge non-reconciling figures (Alpha #224). No LLM, no
  re-analysis.
- **EDGAR coverage 6 → 20 concepts** — turns ~10 more lines PARTIAL → VERIFIED
  against SEC XBRL (Alpha #225). Deterministic.
- **EDGAR name resolution by ticker + brand alias** — `google` → Alphabet (Alpha
  #223); re-landed cell-selection fix #70 (Parser #74 + backend contract #226).

This doc covers what remains: an LLM-driven **research corroboration** pass that
reaches the figures/claims the deterministic layer cannot, **without dying when
the Anthropic account caps**.

## Goals / non-goals

**Goals**

- Independently corroborate financial figures XBRL does not tag, market sizing,
  and qualitative claims by *thoroughly reading public sources*.
- Survive an Anthropic cap/outage.

**Non-goals / hard constraints**

- **Accuracy is paramount.** Every agent-sourced number carries an exact source
  URL + quote. The agent **corroborates or flags**; it never overwrites an
  extracted figure and never fabricates one.
- **Deterministic-first.** #224 (verifier), #225 (EDGAR XBRL), and the registry
  sources run first and stand alone. The agent fills the gaps and adversarially
  verifies; it seeds/augments the deterministic backstop, never replaces it.
- **Best-effort.** Runs in the corroboration job phase; any failure still lets
  the pipeline finish (existing contract).

## Architecture — two layers, in order

### Layer 1 — Provider failover (prerequisite; also un-darkens market sizing)

Every LLM call today hits one Anthropic account, so a cap dark-outs extraction,
deal-profile, dashboard, screening, market web-sizing, and web corroboration at
once. Abstract the call behind a provider interface with fallback.

- **Interface** `LLMProvider`, covering the two call shapes in use:
  - structured output (`messages.parse`) — parser extract, `deal_profile`,
    `dashboard`, `screen_criteria`, `verify`;
  - the web-search tool call — `web_search_collect._call_web_search`.
- **Failover trigger**: reuse the cap detector from Parser #72
  (`AnthropicCreditExhausted`: HTTP 402, `"credit balance is too low"`, or
  `"reached your specified API usage limits"`) → fall through to the next
  provider. A transient 429/5xx stays on the SDK's own retry, as today.
- **Providers**, in accuracy-safety order:
  1. **Claude on AWS Bedrock / Google Vertex** — *same model, same prompts,
     separate quota/billing*. Near-zero accuracy risk; the recommended primary
     fallback. (Zero-integration stopgap if no AWS/GCP account exists: a second
     Anthropic key/org — same model, separate quota.)
  2. **Managed Qwen** (DeepInfra / Together / Fireworks) — cheaper and fully
     independent, but a different model → **requires an accuracy validation pass
     on a real deal and degraded-mode marking** before it is trusted.
- **Plug-in points**: `parser_service/llm_client.py`
  (`make_client` / `parse_with_retry`) and
  `app/services/web_search_collect.py::_call_web_search`, plus the
  `messages.parse` call sites (`deal_profile` / `dashboard` / `screen_criteria` /
  `verify`).
- **Config-driven** per-call-type provider lists; every failover is logged.
- **Effort: medium. Blast radius: large** — *every* Claude-dependent tier
  survives a cap, not just this feature. This is the durable fix for the incident
  that motivated the whole effort, and it is shared with the market-size spec's
  Layer 1.

### Layer 2 — The research agent (rides Layer 1)

A bounded per-deal research pass that mints corroboration events / `kind="web"`
claims through the **existing** `persist_corroboration` / `persist_web_facts`
seams — so the UI, the "Public source" provenance badge (Web #68), and the
roll-up already render its output.

- **Targets, gated by the deterministic layer**: for each figure/claim still
  uncorroborated after #225 + the registry sources, and anything #224 flagged as
  not-reconciling, the agent researches it. It never spends a call on something
  already corroborated deterministically.
- **Sources**: reuse `DEFAULT_ALLOWED_DOMAINS` (sec.gov + market-research
  houses); add **EDGAR full-text** (Item 1 Business / Item 7 MD&A / Item 8 notes)
  for the figures and market/industry narrative XBRL misses.
- **Shape**: gather → **adversarial verify** (a second pass tries to refute each
  candidate; keep only survivors carrying an exact URL + quote) → record.
- **Bounded**: per-deal token/time budget; async, best-effort; never blocks the
  chain.
- **Provenance**: mints `kind="web"` → existing badge + web-first ordering.
- **Effort: large.** Phase it:
  - **2a — financials corroboration** (EDGAR full-text + optional market research
    for the ~unmapped lines).
  - **2b — qualitative** (competitors / customers / market claims).

## Recommended decisions

1. **Fallback provider: Claude on Bedrock/Vertex.** Identical model → identical
   output → zero accuracy re-validation. Qwen is a larger, riskier project for
   marginal savings on an occasional fallback; defer. Second Anthropic key is the
   zero-integration stopgap.
2. **Orchestration: inline bounded async loop** in the corroboration job with a
   small adversarial-verify fan-out. Ship the simple version before investing in
   heavier orchestration.
3. **Scope of 2a: EDGAR full-text first.** Free, authoritative, no
   licensing/paywall/accuracy baggage. Add market-research sources later if the
   gap warrants.

## Sequencing

1. **Ship + deploy the deterministic set** (#224/#225/#226→#74/#223) and
   **re-analyze one deal** to confirm on real data. (Prerequisite — validate
   before building more.)
2. **Layer 1 — provider failover** (Bedrock/Vertex). Durable cap fix; also
   restores market sizing + web corroboration.
3. **Layer 2a — financials research**, on Layer 1.
4. **Layer 2b — qualitative research.**

## Risks / open questions

- **Provider creds**: Bedrock (AWS) / Vertex (GCP) need an account; the platform
  is otherwise on DigitalOcean. Confirm which cloud, or use the second-key
  stopgap.
- **Qwen accuracy** (if chosen later): must be validated on a real deal and
  marked degraded.
- **Cost/latency** of the research pass: enforce a per-deal budget; keep it
  best-effort and off the critical path.
- **Not a substitute for lifting the cap**: Layer 1 is resilience, not free
  capacity — a depleted account still needs funding/limit changes.
