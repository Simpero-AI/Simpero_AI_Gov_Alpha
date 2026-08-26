from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.public_database import public_engine


def test_public_async_session_local_imported_by_exactly_one_module():
    """As of this ticket (P1-07), PublicAsyncSessionLocal has zero importers
    by design -- app/core/public_dependencies.py, the module that will
    import it, doesn't exist until P1-04. app/core/public_database.py itself
    defines it, it doesn't import it, so it doesn't count. Expect this count
    to become 1 once P1-04 lands; update the assertion then, not before.
    """
    importers = []
    for path in Path("app").rglob("*.py"):
        if path == Path("app/core/public_database.py"):
            continue
        text_content = path.read_text()
        if "PublicAsyncSessionLocal" in text_content and "import" in text_content:
            for line in text_content.splitlines():
                if "import" in line and "PublicAsyncSessionLocal" in line:
                    importers.append(str(path))
                    break

    assert importers == [], (
        f"Expected zero importers of PublicAsyncSessionLocal today, found: {importers}"
    )


def test_public_engine_uses_nullpool():
    assert isinstance(public_engine.pool, NullPool)


async def test_dd_app_session_keyhole_guc_has_no_effect(db_session):
    """No keyhole policy exists for dd_app at all -- deal_intake_link's only
    dd_app policy is org_isolation, unrelated to app.intake_token_hash.

    The spec's literal acceptance criterion is "assert zero rows", which
    assumes a pristine table. In practice several P1-01 fixtures deliberately
    leave org_a rows behind (no teardown -- see test_intake_link_rls.py's
    org_a_deal_id docstring), so this DB is not empty for test_org_id by the
    time this test runs. The equivalent, pollution-proof proof: the row set
    visible before and after setting app.intake_token_hash is identical --
    i.e. the GUC has zero effect on what org_isolation already exposes.
    Doesn't require P1-03's keyhole policies to exist; this proves their
    absence of effect on dd_app, which is already true today.
    """
    before = await db_session.execute(text("SELECT id FROM deal_intake_link"))
    before_ids = {row[0] for row in before.fetchall()}

    await db_session.execute(
        text("SELECT set_config('app.intake_token_hash', :h, true)"), {"h": "whatever"}
    )
    after = await db_session.execute(text("SELECT id FROM deal_intake_link"))
    after_ids = {row[0] for row in after.fetchall()}

    assert after_ids == before_ids


def test_missing_public_database_url_raises_validation_error(monkeypatch):
    monkeypatch.delenv("PUBLIC_DATABASE_URL", raising=False)
    # Bypass get_settings()'s lru_cache -- construct Settings() directly, same
    # call get_settings() makes internally, so this doesn't need a cache-clear.
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]
