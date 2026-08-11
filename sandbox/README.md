# Local sandbox

Run the whole pipeline on your own machine — with **no cloud database**. No DigitalOcean cluster, no firewall rule. Two modes, each its own UI:

- **Parsing** — a CIM goes in, cited claims land in a local Postgres, and an interactive HTML **claims report** opens in your browser.
- **Retrieval** — upload a CIM, ask a question, and see the **chunks** the hybrid search returns (dense + sparse), live in a small web app.

```
./sandbox/run.sh path/to/cim.pdf    # PARSING → parse → extract → ingest → reconcile → opens the claims report
                                    #   (auto-brings the sandbox up on the first run — one command)
./sandbox/run.sh path/to/cim.pdf --audit   # …and re-check each figure against its cited span
./sandbox/retrieve.sh               # RETRIEVAL → serve the upload / ask / chunks UI
./sandbox/down.sh                   # stop  (--wipe also deletes the data)
# ./sandbox/up.sh                   # optional: bring the DB up on its own (run.sh does this for you)
```

The infrastructure needs no credentials. **Extraction has two tiers, and the prose tier does:** table extraction is deterministic and offline, but reading facts out of prose calls the Anthropic API, so the default run needs `ANTHROPIC_API_KEY` (see [§3](#3-run-the-pipeline-on-a-cim)). Pass `--tables-only` for a fully offline, key-free run.

These instructions are verified end-to-end on macOS (Apple Silicon) with Colima.

---

## 1. Prerequisites

You need **uv**, a **Docker runtime**, and **both repos cloned side by side**.

### 1a. uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 1b. A Docker runtime

Either works. **Colima** is recommended for a terminal workflow — it's free, needs no GUI, and no license. (Docker Desktop is fine too if you already have it; skip to 1c.)

```bash
brew install colima docker docker-compose

# Homebrew does not auto-link the compose plugin. Link it once so `docker compose` works:
mkdir -p ~/.docker/cli-plugins
ln -sfn "$(brew --prefix)/lib/docker/cli-plugins/docker-compose" ~/.docker/cli-plugins/docker-compose

# Start the Docker VM (first run downloads a small Linux image; a few minutes):
colima start
```

Confirm the daemon is up:

```bash
docker info    # should print "Server Version: ..." with no "Cannot connect" error
```

### 1c. Both repos, side by side

The pipeline is two services. The sandbox lives in this backend repo and calls the parse service, which it expects as a **sibling directory**:

```
your-projects/
  Simpero_AI_Gov_Alpha/          ← this repo
  Simpero_Gov_AI_Services/       ← the parse service (git clone it here)
```

```bash
# from the directory that contains Simpero_AI_Gov_Alpha:
git clone https://github.com/Digitallick/Simpero_Gov_AI_Services.git

# install deps in BOTH repos, once:
( cd Simpero_AI_Gov_Alpha    && uv sync --all-extras --dev )
( cd Simpero_Gov_AI_Services && uv sync --all-extras --dev )
```

If you keep the parse service somewhere else, set `PARSER_REPO=/path/to/Simpero_Gov_AI_Services` before running `run.sh`.

---

## 2. Bring it up

```bash
cd Simpero_AI_Gov_Alpha
./sandbox/up.sh
```

This pulls `pgvector/pgvector:pg16` (stock Postgres 16 with the pgvector extension bundled — the cloud database has pgvector, so the sandbox matches it) and `valkey:8`, starts them, waits until they're healthy, and applies the migrations. You should see:

```
==> starting Postgres + Valkey
 Container simpero-sandbox-postgres-1  Healthy
 Container simpero-sandbox-valkey-1    Healthy
==> applying migrations (as doadmin)
 Running upgrade aace95a1c412 -> 60a151dd80b0, claims spine
==> sandbox is up.
    Postgres : localhost:5433  (db simpero, roles doadmin / dd_app)
    Valkey   : localhost:6380
```

`up.sh` is idempotent — safe to re-run.

---

## 3. Run the pipeline on a CIM

```bash
# the full pipeline (default) — needs a key, see below:
export ANTHROPIC_API_KEY=sk-ant-...
./sandbox/run.sh /path/to/your-cim.pdf --entity "Target Co"

# or a fully offline, key-free run:
./sandbox/run.sh /path/to/your-cim.pdf --entity "Target Co" --tables-only

# add --audit to re-check each figure against its own cited span (needs a key):
./sandbox/run.sh /path/to/your-cim.pdf --entity "Target Co" --audit
```

- `--entity` names the company the claims are about (optional; defaults to "Target Co").
- `--org` sets the demo tenant key (optional; defaults to `sandbox_demo`).
- `--audit` runs the binding auditor after extraction (opt-in; see below).

**Extraction tiers** — each a strict superset of the one above it:

| flag | tiers | model calls | key |
|---|---|---|---|
| `--tables-only` | tables | none | not needed |
| `--prose` | + numeric facts in prose | 1 / prose page | **required** |
| `--qualitative` *(default)* | + claims that carry no number | 2 / prose page | **required** |

The prose tiers call the Anthropic API and require **`ANTHROPIC_API_KEY`** (or `ANTHROPIC_AUTH_TOKEN`) exported in your shell. `run.sh` checks for it up front and stops before touching your CIM if it is absent. The key is **never** read from `sandbox/.env.sandbox` — that file is committed, and an API key does not belong in it; it comes from your environment only. Your parse-service checkout must be recent enough to have the prose tiers (the `--prose`/`--qualitative` flags on `emit_claims.py`); `git pull` it if `--tables-only` works but the prose tiers report an unknown flag.

**Binding audit (`--audit`, optional).** Pass `--audit` to run the binding auditor after extraction: it re-reads each figure that carries a page-header scale against its *own* cited span and flags `binding_unsupported` — with a mode (`attribute_mismatch`, `basket_collapse`, `scale_absurd`, …) — when the span does not actually support the value. It is **flag-only**: it never changes a claim's status or value. It makes one model call per audited figure, so it needs the API key just like the prose tiers. Off by default; when on, it fills the report's **Binding-audit panel** ([§4](#4-the-html-report)).

**Confidentiality:** the CIM you pass is copied into `sandbox/cim/`, which is **gitignored and never committed**. Real deal documents are confidential — don't commit them. The prose tiers additionally **send each prose page's text to the Anthropic API**, so a real deal document leaves your machine on those tiers; `--tables-only` makes no network call at all. No sample document ships in this repo; bring your own (any financial-statement PDF with a table under a scale header works well — income statements are ideal).

`run.sh` narrates the six steps demo-style — a live spinner with elapsed time during the parse (the slow step on long documents), then a summary of what landed instead of a raw table dump — and finishes by opening the HTML report in your browser (counts below are illustrative; yours will differ):

```
[1/6] Copying the CIM into sandbox/cim/  (gitignored, never committed)   ✓
[2/6] Parse → extract → emit   (tables + prose + qualitative; docling layout analysis)
      ⠹ analyzing the document… 23s          ← spinner, replaced on completion by:
      ✓ emitted 576 claims (573 resolved, 3 missing), 41 flags
[3/6] Ingest into the local claims spine   (backend, as the dd_app app role)
      576 claims validated against the contract.
      dd_app, tenant 'sandbox_demo': sees 576 claims (inserted 576).
      dd_app, a DIFFERENT tenant: sees 0 claims (RLS isolation).
[4/6] Reconcile + check consistency   (3a same-fact, 3b formula reconstruction → edges)
      runs the verification passes on THIS tenant's claims; writes claim edges + flags.
      3a reconciliation (same fact across pages/tiers):
            same_fact edges .......... 18   (same figure on >1 page, agreeing)
            contradicts edges ........ 4    (same fact, two different numbers)
            claims flagged ........... 18
      3b consistency (formula reconstruction):
            derived_from edges ....... 2    (a total rebuilt from its own operands)
            skipped (missing operands) 7    (--tables-only writes no computational claims)
[5/6] Reading the claims back   (this run's claims, from Postgres)
      Claims stored ............ 576
      With exact citation ...... 573   (page + character span + word-level boxes)
      Missing citation ......... 3     (nothing resolved — recorded honestly, no fabrication)
      Distinct attributes ...... 41
      Pages with claims ........ 37    (pages 5-49 of the document)
[6/6] Building the interactive report   (rendered pages + click-through citations)
```

Read it top to bottom:

- The `dd_app sees 576 / a different tenant sees 0` pair is the **tenant isolation proof** — enforced by Postgres row-level security, exercised as the `dd_app` app role.
- **Step 4 is the verification layer.** It runs on *this tenant's* claims and writes claim-to-claim **edges** (and flags), never mutating a claim's status: `same_fact` corroborates a figure across pages, `contradicts` marks two numbers for one fact, `derived_from` records a total the parser rebuilt from its own operands. 3b (`derived_from`) needs *computational* claims, which only the prose/qualitative tiers emit — `--tables-only` writes none.
- **Steps 4 and 5 are both run-scoped** by `session_id` (SIM-389). `run.sh` mints one id per run and passes it to the ingest *and* the verification pass, so step 4 reconciles only this run's claims and the edges it writes carry that id as their `run_id`. Earlier runs stay in the table without inflating either step's numbers, and a second run no longer needs a wipe first. (The step-3 "tenant sees N" line is still an all-runs total — it is the RLS proof, not a per-run count.) Running `scripts/run_verification.py` by hand without `--session-id` keeps the old all-runs behaviour and says so in its output.
- Every claim carries the raw printed value, the scaled `normalized` number, and an exact character span. **None is fabricated** — see the status note below for what `proposed` / `cited` / `missing` mean.

### What `status` means

The status column is **not** a verification verdict — it records how the value was *read*, and the parser never promotes it:

| status | meaning |
|---|---|
| `proposed` | a figure read from a **PDF** span (or an unresolved spreadsheet formula). The model *proposes* it from the page; the parser will not self-certify a PDF reading, so **every PDF figure is `proposed`** — even with a perfect citation. |
| `cited` | a **directly-read spreadsheet cell** (an `.xlsx` literal). Reading the bytes *is* the verification, so nothing is left to check. PDFs never reach this. |
| `missing` | nothing resolved — no span. Recorded honestly rather than guessed. |

So on a PDF CIM you see a wall of `proposed` and a few `missing`, and **that is correct**. "With exact citation … N" in step 5 counts claims that *resolved to a span* — those are `proposed` claims that carry a citation, which is a different axis from the status word. Promoting a claim to a `verified` status would assert a cross-source check the pipeline deliberately leaves to a later gated pass; here, verification surfaces as the **edges and flags** from step 4 (and the corroborated cue in the report), never as a status change.

For a pristine client demo where every count matches the run, start clean: `./sandbox/down.sh --wipe && ./sandbox/up.sh`.

---

## 4. The HTML report

`run.sh` finishes by rebuilding `sandbox/report.html`'s data and opening it
in your browser (re-open it any time — no server needed). It has two layers:
what was **extracted**, and what **verification** made of it.

**Extraction**

- **Header + stat cards** — entity, source file, tenant, page range, and
  counts for total claims, figures, assertions, missing citation, and distinct
  attributes.
- **Search across every column** — attribute, raw, normalized, unit, scale
  source, page, span, status, flags — with a live "Showing X of Y" count and a
  paginated table (50 rows per page with Prev/Next; the pager hides when
  everything fits). Long attribute names wrap so the other columns stay visible.
- **Click-through citations** — any resolved row has a clickable page number: a
  modal opens the actual rendered PDF page with the clicked claim's word-boxes
  highlighted in gold and every other cited claim on the page outlined in teal
  (Esc / click outside / × closes). `missing` rows stay plain text.

**Verification** (from step 4, plus the binding audit if you passed `--audit`)

- **Verification summary cards** — Corroborated, Flagged claims, Same-fact,
  Contradicts, Derived-from: a quick read on how much of the parse the
  verification layer touched.
- **Filter chips** — alongside the status chips (All / Proposed / Missing) sit
  **Corroborated / Flagged / Contradicts** chips, each shown only when
  non-empty. On a PDF, where every row is `proposed`, these are how you find the
  handful of claims that matter — one click isolates them.
- **Flag + corroborated badges** — the status cell carries any flags (e.g.
  `ambiguous_unit`, `binding?`) and a green **`corroborated ✓`** badge on the
  surviving claim of a `same_fact` pair (or the reconstructed claim of a
  `derived_from` chain) when no contradicts edge and no *integrity* flag
  undercuts it. Rows are bordered to match — green for corroborated, gold for
  flagged. Corroborated is a **signal derived from the edges and flags, not a
  `verified` status** — the parser never stamps one (see the status note in §3).
- **Relationships table** — every claim-to-claim edge the step-4 passes wrote:
  type (`same_fact` / `contradicts` / `derived_from`), the two claims, and the
  basis.
- **Binding-audit panel** — one row per binding-auditor verdict, with its mode
  (`attribute_mismatch`, `basket_collapse`, `scale_absurd`, …) and the evidence.
  **Empty unless you ran with `--audit`** (see §3).

The report always reflects the **latest run** for the tenant (scoped by
`session_id`), not the accumulation of every run before it.

**Confidentiality by construction:** `report.html` is a data-free template,
committed to git. Everything confidential — the claims, the edges, and the page
images, rendered at 144 DPI with pypdfium2 from the parser repo's venv — is
written to `sandbox/cim/report_data.js`, which the template loads via a
`<script>` tag (works from `file://`, no server needed). `cim/` is gitignored,
so no CIM content can leak onto git. Only pages carrying at least one bbox-cited
claim are rendered.

The data file is a static snapshot — re-run `run.sh` to refresh it, or
rebuild just the report without re-parsing (from this repo's root):

```bash
uv run --project ../Simpero_Gov_AI_Services python sandbox/export_report.py \
  --pdf "sandbox/cim/your-cim.pdf"     # --pdf optional when cim/ holds exactly one PDF
```

---

## 5. Retrieval mode — upload a PDF, ask, see the chunks

Parsing mode (above) lands *claims*; retrieval mode is the other half — it cuts a
document into **chunks**, embeds them, and answers a natural-language question with
the most relevant ones through the hybrid dense + sparse search. Unlike the static
claims report, this is interactive, so it is a small served app rather than a file:

```bash
# dense + sparse (recommended): put your Voyage key in the backend .env first
echo 'VOYAGE_API_KEY=...' >> .env       # .env is gitignored — never committed

./sandbox/retrieve.sh                   # starts the app, opens http://localhost:8010
```

Then in the browser:

- **1 · Document** — pick a CIM and click *Ingest*. It parses + chunks the PDF in
  the parse service, embeds each chunk, and stores them scoped to a demo tenant.
  A CIM takes ~a minute (the docling parse); re-uploading replaces the previous
  document's chunks.
- **2 · Ask** — type any question and hit *Search* to see the ranked chunks (score,
  page, element type, text).

**Embeddings are a separate provider from Anthropic** — the query and chunks are
embedded with **voyage-4-large**, so retrieval reads `VOYAGE_API_KEY` (from the
backend `.env`, git-ignored, or your shell), independent of `ANTHROPIC_API_KEY`:

- **with a key** — the page shows a `dense + sparse` badge, and semantic questions
  work even when the wording is absent from the document (e.g. *"how is the business
  performing financially?"* surfaces the EBITDA / CAGR chunks).
- **without a key** — it runs `sparse-only` (keyword search): chunks go in
  unembedded and only exact-term matches return. A full natural-language question
  often comes back empty, because the keyword leg ANDs the words together — set the
  key for real semantic retrieval.

Sandbox only: writes to the LOCAL Postgres, never a cloud DB. Set `PORT=...` before
`retrieve.sh` to use a different port.

---

## 6. Query the claims yourself

The report and step-4 summary cover the common questions; for anything else,
go straight at the database:

```bash
docker compose -f sandbox/docker-compose.yml exec postgres \
  psql -U doadmin -d simpero -c "SELECT attribute, value->>'normalized' FROM claims;"
```

---

## 7. Tear down

```bash
./sandbox/down.sh          # stop the containers, keep the data
./sandbox/down.sh --wipe   # stop and DELETE the local database (clean slate)
colima stop                # stop the Docker VM entirely (frees its ~2 GB RAM)
```

To start over from an empty database: `./sandbox/down.sh --wipe` then `./sandbox/up.sh`.

---

## How this differs from production

Deliberately simplified for local use — all correct, none load-bearing on the cloud:

- **No PgBouncer.** The app connects directly to local Postgres. `SET LOCAL` tenant scoping works fine on a direct connection; the pooler only matters for connection scaling in production.
- **Plain `redis://` Valkey**, not DO's TLS-only `rediss://`.
- **`dd_app` has a well-known local password** (in `sandbox/.env.sandbox`) — a local-only container credential, not a secret.
- **Auth is the same stub as production** (`decode_clerk_jwt`), so the demo sets the tenant context directly rather than decoding a Clerk JWT.

Otherwise it's faithful: the same two Postgres roles (`doadmin` owns and migrates; `dd_app` is RLS-restricted), the same migrations, the same RLS policies, and the same two-process seam — the parse service emits claims as JSON, the backend ingests them, neither sharing a runtime.

---

## Troubleshooting

| symptom | fix |
|---|---|
| `docker: 'compose' is not a docker command` | link the plugin (step 1b): `ln -sfn "$(brew --prefix)/lib/docker/cli-plugins/docker-compose" ~/.docker/cli-plugins/docker-compose` |
| `Cannot connect to the Docker daemon` | `colima start` (or launch Docker Desktop) |
| `error: parse service repo not found` | clone `Simpero_Gov_AI_Services` as a sibling, or set `PARSER_REPO=/path/to/it` |
| `port 5433 / 6380 already in use` | change the host ports in `sandbox/docker-compose.yml` and the matching URLs in `sandbox/.env.sandbox` |
| migrations fail on first `up.sh` | the DB may still be initializing — re-run `./sandbox/up.sh` (it's idempotent), or `./sandbox/down.sh --wipe` and start fresh |
| want a completely clean slate | `./sandbox/down.sh --wipe && ./sandbox/up.sh` |
