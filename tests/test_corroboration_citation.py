"""Unit tests for the corroboration citation-URL resolver
(app/services/corroboration_citation.py). Pure function over a source name + the
event's stored `result` payload; no DB, no network. The per-source result shapes
mirror what each adapter in corroboration_sources/ actually persists."""

from app.services.corroboration_citation import corroboration_source_url


def test_federal_register_uses_the_stored_html_url():
    result = {
        "source": "us_federal_register",
        "document": {
            "document_number": "2024-12345",
            "html_url": "https://www.federalregister.gov/documents/2024/06/01/2024-12345/x",
        },
    }
    assert (
        corroboration_source_url("us_federal_register", result)
        == "https://www.federalregister.gov/documents/2024/06/01/2024-12345/x"
    )


def test_federal_register_without_a_document_url_is_none():
    assert (
        corroboration_source_url("us_federal_register", {"source": "us_federal_register"}) is None
    )
    assert corroboration_source_url("us_federal_register", {"document": {}}) is None


def test_sec_edgar_builds_the_company_page_from_cik_zero_padded():
    url = corroboration_source_url("sec_edgar", {"cik": 320193})
    assert url is not None
    assert "CIK=0000320193" in url
    assert url.startswith("https://www.sec.gov/cgi-bin/browse-edgar")


def test_sec_edgar_accepts_a_numeric_string_cik():
    url = corroboration_source_url("sec_edgar", {"cik": "320193"})
    assert url is not None and "CIK=0000320193" in url


def test_sec_edgar_without_a_cik_is_none():
    assert corroboration_source_url("sec_edgar", {"concept": "Revenues"}) is None
    assert corroboration_source_url("sec_edgar", {"cik": None}) is None


def test_orgbook_bc_links_to_the_human_entity_page():
    result = {"registry": "orgbook_bc", "registry_id": "BC0871426"}
    assert (
        corroboration_source_url("ised_corporations_canada", result)
        == "https://orgbook.gov.bc.ca/entity/BC0871426"
    )


def test_ised_links_to_the_record_api_host():
    result = {"registry": "ised", "registry_id": "1234567"}
    url = corroboration_source_url("ised_corporations_canada", result)
    assert url == "https://ised-isde.canada.ca/cc/lgcy/api/corporations/1234567.json"


def test_ised_without_a_registry_id_is_none():
    assert corroboration_source_url("ised_corporations_canada", {"registry": "ised"}) is None


def test_trademarks_have_no_reliable_permalink():
    result = {"registry": "uspto", "registration_id": "88123456"}
    assert corroboration_source_url("trademarks_cipo_uspto", result) is None


def test_unknown_source_is_none():
    assert corroboration_source_url("some_future_source", {"url": "https://x"}) is None
