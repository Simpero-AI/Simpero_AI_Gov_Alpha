"""CreateMandateCategoryRequest.category defaults from slug's canonical
label but stays overridable, and creation must fail rather than silently
produce a category with no name when neither is given."""

import pytest
from pydantic import ValidationError

from app.schemas.admin.mandate import CreateMandateCategoryRequest, MandateCategorySlug


def test_category_defaults_to_canonical_label_from_slug():
    req = CreateMandateCategoryRequest(slug=MandateCategorySlug.CHECK_SIZE_RANGE)
    assert req.category == "Check Size Range"


def test_explicit_category_overrides_default():
    req = CreateMandateCategoryRequest(
        slug=MandateCategorySlug.CHECK_SIZE_RANGE, category="Custom Label"
    )
    assert req.category == "Custom Label"


def test_no_slug_keeps_explicit_category():
    req = CreateMandateCategoryRequest(category="Something Else")
    assert req.category == "Something Else"
    assert req.slug is None


def test_neither_slug_nor_category_raises():
    with pytest.raises(ValidationError):
        CreateMandateCategoryRequest()
