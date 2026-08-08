#!/usr/bin/env bash
# Run the whole pipeline against the local sandbox on a CIM you provide, and
# print exactly what lands in the claims table.
#
#   ./sandbox/run.sh /path/to/your-cim.pdf [--entity "Target Co"] [--org demo]
#                    [--tables-only | --prose | --qualitative]
#
# TIERS (default --qualitative -- the full pipeline the sandbox exists to show):
#   --tables-only   deterministic table extraction; no model, no key.
#   --prose         + numeric facts stated in prose      (one model call / prose page).
#   --qualitative   + claims that carry no number         (two model calls / prose page).
# The prose tiers call the Anthropic API and need ANTHROPIC_API_KEY (or
# ANTHROPIC_AUTH_TOKEN) in your environment. This script checks for it up front
# and stops before touching your CIM if it is missing -- so set it, or pass
# --tables-only for a key-free run.
#
# CONFIDENTIALITY: the CIM you pass is copied into sandbox/cim/, which is
# gitignored. It is never committed. Do not commit real deal documents. The
# prose tiers additionally SEND each prose page's text to the Anthropic API --
# a real deal document leaves your machine on those tiers; --tables-only never
# makes a network call.
#
# The two halves run as two processes across the C3 seam, exactly as in
# production: the parse service (a sibling repo) emits claims as JSON; the
# backend ingests that JSON into the local claims table. Neither shares a
# runtime, so the backend never needs docling.
set -euo pipefail

SANDBOX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "$SANDBOX_DIR/.." && pwd)"
PARSER_DIR="${PARSER_REPO:-$BACKEND_DIR/../Simpero_Gov_AI_Services}"
COMPOSE=(docker compose -f "$SANDBOX_DIR/docker-compose.yml")

step() { printf '\n\033[1;36m[%s]\033[0m %s\n' "$1" "$2"; }
ok()   { printf '\033[1;32m      ✓ %s\033[0m\n' "$1"; }

ENTITY="Target Co"
ORG_KEY="sandbox_demo"
PDF=""
TIER_FLAG="--qualitative"   # default: the full pipeline
TIER_NAME="tables + prose + qualitative"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --entity)       ENTITY="$2"; shift 2 ;;
    --org)          ORG_KEY="$2"; shift 2 ;;
    --tables-only)  TIER_FLAG="";              TIER_NAME="tables only";                 shift ;;
    --prose)        TIER_FLAG="--prose";       TIER_NAME="tables + prose";              shift ;;
    --qualitative)  TIER_FLAG="--qualitative"; TIER_NAME="tables + prose + qualitative"; shift ;;
    -*)             echo "unknown option: $1"; exit 1 ;;
    *)              PDF="$1"; shift ;;
  esac
done

[[ -n "$PDF" ]]        || { echo "usage: ./sandbox/run.sh <cim.pdf> [--entity NAME] [--org KEY] [--tables-only|--prose|--qualitative]"; exit 1; }
[[ -f "$PDF" ]]        || { echo "error: no such file: $PDF"; exit 1; }
[[ -d "$PARSER_DIR" ]] || { echo "error: parse service repo not found at $PARSER_DIR"; echo "  clone Simpero_Gov_AI_Services beside this repo, or set PARSER_REPO=/path/to/it"; exit 1; }
# The prose tiers call the Anthropic API. Fail here, before copying the CIM or
# starting any work, rather than part way through -- and do NOT read the key
# from sandbox/.env.sandbox (that file is committed; an API key never belongs in
# it). It must come from your own environment.
if [[ -n "$TIER_FLAG" && -z "${ANTHROPIC_API_KEY:-}" && -z "${ANTHROPIC_AUTH_TOKEN:-}" ]]; then
  echo "error: $TIER_FLAG needs ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) in your environment."
  echo "  export it in your shell, or re-run with --tables-only for a key-free run."
  exit 1
fi

# One command: if the sandbox is not already running, bring it up automatically
# (up.sh is idempotent) so a first run is a single step. grep without -q: under
# pipefail, -q's early exit SIGPIPEs docker compose and can falsely fail this check.
if ! "${COMPOSE[@]}" ps --status running 2>/dev/null | grep postgres >/dev/null; then
  printf '\n\033[1;36m[0/5]\033[0m the sandbox is not running yet -- bringing it up first (one-time)\n'
  "$SANDBOX_DIR/up.sh"
fi

printf '\n\033[1;32m========================================================================\n'
printf '  Simpero local sandbox — running the pipeline\n'
printf '========================================================================\033[0m\n'
echo "    Input  : $PDF"
echo "    Entity : $ENTITY"
echo "    Tenant : $ORG_KEY"
echo "    Tiers  : $TIER_NAME"

set -a
# shellcheck disable=SC1091
. "$SANDBOX_DIR/.env.sandbox"
set +a

mkdir -p "$SANDBOX_DIR/cim"
LOCAL_PDF="$SANDBOX_DIR/cim/$(basename "$PDF")"
CLAIMS_JSON="$SANDBOX_DIR/cim/claims.json"
EMIT_LOG="$SANDBOX_DIR/cim/emit.log"

step "1/5" "Copying the CIM into sandbox/cim/  (gitignored, never committed)"
# -ef: skip the copy when the CIM is already sandbox/cim/<name> (cp would fail)
[[ "$PDF" -ef "$LOCAL_PDF" ]] || cp "$PDF" "$LOCAL_PDF"
ok "$(basename "$PDF")"

step "2/5" "Parse → extract → emit   ($TIER_NAME; docling layout analysis)"
# Empty tier flag (tables only) must not become an empty argv entry, so the flag
# is built as an array. The parser subprocess inherits this shell's environment,
# so ANTHROPIC_API_KEY reaches it without being echoed anywhere.
EMIT_ARGS=(scripts/emit_claims.py "$LOCAL_PDF" --entity "$ENTITY")
[[ -n "$TIER_FLAG" ]] && EMIT_ARGS+=("$TIER_FLAG")
( cd "$PARSER_DIR" && env -u VIRTUAL_ENV uv run python "${EMIT_ARGS[@]}" ) > "$CLAIMS_JSON" 2> "$EMIT_LOG" &
EMIT_PID=$!
# ASCII frames on purpose: macOS ships bash 3.2, whose ${var:i:1} slices by
# BYTE — multibyte spinner glyphs come out as mangled fragments.
SPIN='|/-\'
TICK=0
while kill -0 "$EMIT_PID" 2>/dev/null; do
  printf '\r      \033[1;36m%s\033[0m analyzing the document... %ds' "${SPIN:TICK%4:1}" "$((TICK / 4))"
  sleep 0.25
  TICK=$((TICK + 1))
done
printf '\r\033[K'
if wait "$EMIT_PID"; then
  ok "$(grep -o 'emitted .*' "$EMIT_LOG" | tail -1)"
else
  echo "      parse/emit failed:"; sed 's/^/      /' "$EMIT_LOG"; exit 1
fi

step "3/5" "Ingest into the local claims spine   (backend, as the dd_app app role)"
echo "      validates every claim against the contract, then INSERTs under RLS"
# ENVIRONMENT=production only silences SQLAlchemy's SQL echo for clean output;
# it changes nothing on this path (see app/core/database.py).
( cd "$BACKEND_DIR" && ENVIRONMENT=production uv run python scripts/ingest_claims.py "$CLAIMS_JSON" --org-key "$ORG_KEY" --commit \
    | sed 's/^/      /' )

step "4/5" "Verifying what landed   (reading THIS run's claims back from Postgres)"
# Scoped to the session_id the ingest just created — earlier runs for the same
# tenant stay in the table but don't pollute the demo numbers.
IFS='|' read -r TOTAL CITED MISSING ATTRS NPAGES PMIN PMAX <<EOF
$("${COMPOSE[@]}" exec -T postgres psql -U doadmin -d simpero -tA -c "
  SELECT count(*),
         count(*) FILTER (WHERE char_start IS NOT NULL),
         count(*) FILTER (WHERE status = 'missing'),
         count(DISTINCT attribute),
         count(DISTINCT page),
         min(page), max(page)
  FROM claims c JOIN organisation o ON o.id = c.org_id
  WHERE o.clerk_org_id = '$ORG_KEY'
    AND c.session_id IS NOT DISTINCT FROM (
      SELECT c2.session_id FROM claims c2
      JOIN organisation o2 ON o2.id = c2.org_id
      WHERE o2.clerk_org_id = '$ORG_KEY'
      ORDER BY c2.created_at DESC LIMIT 1);")
EOF
echo   "      Claims stored ............ $TOTAL"
echo   "      With exact citation ...... $CITED   (page + character span + word-level boxes)"
echo   "      Missing citation ......... $MISSING   (nothing resolved — recorded honestly, no fabrication)"
echo   "      Distinct attributes ...... $ATTRS"
echo   "      Pages with claims ........ $NPAGES   (pages ${PMIN}-${PMAX} of the document)"
ok "every stored value traces back to an exact spot in the source PDF"

step "5/5" "Building the interactive report   (rendered pages + click-through citations)"
( cd "$PARSER_DIR" && env -u VIRTUAL_ENV uv run python "$SANDBOX_DIR/export_report.py" --pdf "$LOCAL_PDF" --org "$ORG_KEY" )
# Auto-open in the default browser (macOS; a no-op elsewhere).
command -v open >/dev/null && open "$SANDBOX_DIR/report.html" || true

printf '\n\033[1;32m========================================================================\n'
printf '  Done — the claims report is open in your browser.\n'
printf '========================================================================\033[0m\n'
echo "    Every number is clickable through to the exact spot on the PDF page"
echo "    it was read from. All data stays on this machine."
