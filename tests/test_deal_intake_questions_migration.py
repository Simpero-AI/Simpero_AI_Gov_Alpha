"""DB-level constraint coverage for deal_intake_questions (P2-01), added in
response to review on PR #107: nothing in the original diff exercised the
partial-unique-index / CHECK-constraint behavior in CI, so a later
migration touching this table could silently regress the one invariant
this ticket exists to create.

Deliberately talks to the DB directly (owner_conn), not through any API --
this pins the schema's own guarantees, independent of whether P2-02's
router happens to enforce the same thing at the application layer.
"""

from collections.abc import Iterator
from typing import Any

import psycopg2
import psycopg2.errors
import pytest


@pytest.fixture(autouse=True)
def _clean_intake_questions(owner_conn) -> Iterator[None]:
    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM deal_intake_questions")
    yield
    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM deal_intake_questions")


def _insert(owner_conn, question_key: str, is_active: bool = True, **fields: Any) -> str:
    columns = {
        "question_key": question_key,
        "prompt": "A prompt",
        "input_type": "text",
        "display_order": 0,
        "is_active": is_active,
        **fields,
    }
    cols = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    with owner_conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO deal_intake_questions ({cols}) VALUES ({placeholders}) RETURNING id",
            list(columns.values()),
        )
        return str(cur.fetchone()[0])


def test_two_active_rows_cannot_share_a_question_key(owner_conn):
    _insert(owner_conn, "company_name", is_active=True)

    with pytest.raises(psycopg2.errors.UniqueViolation), owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deal_intake_questions "
            "(question_key, prompt, input_type, display_order, is_active) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("company_name", "Duplicate?", "text", 1, True),
        )


def test_deactivating_frees_the_key_for_a_new_active_row(owner_conn):
    first_id = _insert(owner_conn, "company_name", is_active=True)

    with owner_conn.cursor() as cur:
        cur.execute("UPDATE deal_intake_questions SET is_active = false WHERE id = %s", (first_id,))

    second_id = _insert(owner_conn, "company_name", is_active=True)

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, is_active FROM deal_intake_questions "
            "WHERE question_key = 'company_name' ORDER BY is_active"
        )
        rows = cur.fetchall()
    assert rows == [(first_id, False), (second_id, True)]


def test_two_inactive_rows_may_share_a_key(owner_conn):
    """The partial index only scopes active rows -- two deactivated rows
    with the same key are not a collision."""
    _insert(owner_conn, "company_name", is_active=False)
    _insert(owner_conn, "company_name", is_active=False)

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM deal_intake_questions WHERE question_key = 'company_name'"
        )
        assert cur.fetchone()[0] == 2


def test_input_type_check_constraint_rejects_unsupported_values(owner_conn):
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert(owner_conn, "company_name", input_type="dropdown")


def test_input_type_check_constraint_accepts_the_supported_set(owner_conn):
    _insert(owner_conn, "text_question", input_type="text")
    _insert(owner_conn, "textarea_question", input_type="textarea")

    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM deal_intake_questions")
        assert cur.fetchone()[0] == 2
