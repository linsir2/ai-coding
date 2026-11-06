"""The unified tool-error contract: 16 gRPC families + a legacy detail.

The registry is the only source of truth.  These tests pin the four things a
consumer relies on:

* the family set is the standard gRPC one (closed, so the UI can label all 16);
* ``ToolResult`` normalises every input into ``(family, detail)``;
* the fatigue ladder keeps the behaviour measured against the old leaf codes;
* the frontend's label table covers every family.
"""

from __future__ import annotations

import re
from pathlib import Path

from ai_coding.domain.error_codes import (
    CANCELLED_BY_USER,
    LEGACY_TO_FAMILY,
    ToolError,
    family_of,
    is_family,
    is_neutral,
    is_retryable,
    normalize,
    severity_of,
    tone_of,
)

ROOT = Path(__file__).resolve().parents[1]
TOOLMETA = ROOT / "ai_coding" / "frontend" / "src" / "util" / "toolmeta.ts"

GRPC_FAMILIES = {
    "CANCELLED",
    "UNKNOWN",
    "INVALID_ARGUMENT",
    "DEADLINE_EXCEEDED",
    "NOT_FOUND",
    "ALREADY_EXISTS",
    "PERMISSION_DENIED",
    "RESOURCE_EXHAUSTED",
    "FAILED_PRECONDITION",
    "ABORTED",
    "OUT_OF_RANGE",
    "UNIMPLEMENTED",
    "INTERNAL",
    "UNAVAILABLE",
    "DATA_LOSS",
    "UNAUTHENTICATED",
}


def test_the_family_set_is_the_standard_grpc_one():
    assert {member.value for member in ToolError} == GRPC_FAMILIES
    for family in GRPC_FAMILIES:
        assert is_family(family)
    assert not is_family("stale_read")
    assert not is_family(None)


def test_every_legacy_leaf_maps_to_a_family():
    for leaf, family in LEGACY_TO_FAMILY.items():
        assert isinstance(family, ToolError), leaf
        assert family.value in GRPC_FAMILIES


def test_success_keeps_an_empty_pair():
    assert normalize(None, success=True) == (None, None)


def test_an_unclassified_failure_is_internal():
    assert normalize(None, success=False) == ("INTERNAL", None)


def test_a_legacy_leaf_becomes_family_plus_detail():
    assert normalize("stale_read", success=False) == ("FAILED_PRECONDITION", "stale_read")


def test_a_family_emitted_directly_has_no_detail():
    assert normalize("PERMISSION_DENIED", success=False) == ("PERMISSION_DENIED", None)


def test_the_enum_member_is_a_family_too():
    """``ToolError`` is a str mixin whose ``str()`` is "ToolError.X".

    A producer that passes the member (the module exports it for exactly that)
    must not fall into the UNKNOWN bucket.
    """
    assert normalize(ToolError.INTERNAL, success=False) == ("INTERNAL", None)
    assert family_of(ToolError.DEADLINE_EXCEEDED) == "DEADLINE_EXCEEDED"
    assert is_family(ToolError.NOT_FOUND)


def test_an_unregistered_string_is_unknown_and_keeps_the_original():
    assert normalize("brand_new_code", success=False) == ("UNKNOWN", "brand_new_code")


def test_family_of_tolerates_leaves_and_none():
    assert family_of("timeout") == "DEADLINE_EXCEEDED"
    assert family_of("NOT_FOUND") == "NOT_FOUND"
    assert family_of(None) == "UNKNOWN"


def test_a_bare_family_uses_its_default_severity():
    assert severity_of("DEADLINE_EXCEEDED") == 5
    assert severity_of("FAILED_PRECONDITION") == 2
    assert severity_of("INTERNAL") == 5


def test_a_detail_keeps_the_legacy_keyword_severity():
    # The ladder was tuned against these words, so a legacy pair must not move.
    assert severity_of(None, "tool_timeout") == 5
    assert severity_of(None, "file not found") == 2
    assert severity_of(None, "denied") == 1
    # The historical keyword function also saw the leaf code, underscores
    # included, so it returned 1 for this one -- keep it.
    assert severity_of("FAILED_PRECONDITION", "old_string_not_found") == 1


def test_only_the_two_historical_exemptions_are_neutral():
    assert is_neutral("CANCELLED", CANCELLED_BY_USER)
    assert is_neutral("FAILED_PRECONDITION", "invalid_plan")
    assert not is_neutral("PERMISSION_DENIED", "guard_denied")
    assert not is_neutral("CANCELLED", None)


def test_retryability_follows_the_family():
    assert is_retryable("DEADLINE_EXCEEDED")
    assert is_retryable("UNAVAILABLE")
    assert is_retryable("ABORTED")
    assert not is_retryable("FAILED_PRECONDITION")
    assert not is_retryable("INVALID_ARGUMENT")


def test_tone_matches_the_ui_s_denied_set():
    assert tone_of("PERMISSION_DENIED") == "denied"
    assert tone_of("CANCELLED") == "denied"
    assert tone_of("FAILED_PRECONDITION") == "denied"
    assert tone_of("INTERNAL") == "failed"
    assert tone_of("UNAVAILABLE") == "failed"


def test_the_frontend_labels_every_family():
    text = TOOLMETA.read_text(encoding="utf-8")
    block = text.split("const FAILURES:", 1)[1].split("\n}", 1)[0]
    keys = set(re.findall(r"^\s{2}(\w+):", block, flags=re.MULTILINE))
    assert keys == GRPC_FAMILIES
