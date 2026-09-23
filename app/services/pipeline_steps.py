# Ported from Simpero_AI_Gov_Web's src/shared/pipelineSteps.ts -- must stay
# in sync with that file's PIPELINE_STEPS list.
#
# The phases `current_phase` can actually report, each backed by a job that
# really runs: "parsing" (start_deal_analysis), "verification" (parsing
# successful / start_deal_verification running), and "analysis" (the chained
# corroboration + screening runs after verification). Only stages a job sets are
# listed -- the previous 9-entry list included phases ("classify", "pass1",
# "ofac", ...) no job ever set, so _steps_for_status's index-based status marked
# them "done" once current_phase moved past their position, telling the user
# stages ran that never did.
#
# "governance" is intentionally NOT a list entry: it is the terminal marker
# (screening/verification successful, nothing running), represented as every
# listed step being "done" rather than a step of its own -- see _steps_for_status
# in app/api/deals.py. "analysis", by contrast, IS a real running step:
# corroboration has no analysis_run row of its own, so this one step stands for
# the whole post-verification chain (corroboration against outside sources, then
# mandate screening), shown "current" while the screening run is in flight.

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
    {
        "phase": "analysis",
        "title": "Corroboration & analysis",
        "detail": "Checking claims against outside sources and screening the deal",
    },
]


def no_job_steps() -> list[dict[str, str]]:
    """computeStepStatuses(null, false) equivalent: every step "pending"."""
    return [{**step, "status": "pending"} for step in PIPELINE_STEPS]
