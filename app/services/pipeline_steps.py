# Ported from Simpero_AI_Gov_Web's src/shared/pipelineSteps.ts -- must stay
# in sync with that file's PIPELINE_STEPS list.
#
# Reduced (2026-08-12) to the two phases `current_phase` can actually ever
# report -- "parsing" (start_deal_analysis) and "verification" (parsing successful /
# start_deal_verification running). The previous 9-entry list included
# phases ("classify", "pass1", "ofac", "pass3_compose", "pass4_score",
# "finalize") no job has ever set, so _steps_for_status's index-based status
# assignment marked them "done" whenever current_phase moved past their
# position -- a real bug, not just noise, since it told the user stages ran
# that never did. "governance" (verification successful) is intentionally
# NOT a list entry either: nothing is actively running once it's reached,
# so it's represented as every listed step being "done", not as a step of
# its own -- see _steps_for_status in app/api/deals.py.

PIPELINE_STEPS: list[dict[str, str]] = [
    {
        "phase": "parsing",
        "title": "Parsing & extracting",
        "detail": "Reading the document and extracting claims",
    },
    {
        "phase": "verification",
        "title": "Verifying claims",
        "detail": "Cross-checking and reconciling extracted claims against the source",
    },
]


def no_job_steps() -> list[dict[str, str]]:
    """computeStepStatuses(null, false) equivalent: every step "pending"."""
    return [{**step, "status": "pending"} for step in PIPELINE_STEPS]
