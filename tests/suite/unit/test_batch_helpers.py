# tests/suite/unit/test_batch_helpers.py
# =============================================================================
# Pure helpers in batch.py: URL collection from JSON/CSV/TSV/text, selector
# validation, job counting and user-facing error messages.
# =============================================================================
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import batch
from batch import BatchSubmitRequest


# ------------------------------------------------------------- URL collection
def test_collect_from_list_dedupes_and_keeps_order():
    req = BatchSubmitRequest(urls=["https://b.test", "https://a.test", "https://b.test"])
    assert req.collect_urls() == ["https://b.test", "https://a.test"]


def test_collect_drops_non_http_schemes():
    req = BatchSubmitRequest(urls=[
        "https://ok.test", "ftp://no.test", "javascript:alert(1)",
        "file:///etc/passwd", "  ", "ok.test",
    ])
    assert req.collect_urls() == ["https://ok.test"]


@pytest.mark.parametrize("text", [
    "https://a.test,https://b.test\nhttps://c.test",
    "https://a.test\thttps://b.test\nhttps://c.test",
    "https://a.test\nhttps://b.test\n\nhttps://c.test\n",
])
def test_collect_from_csv_tsv_and_lines(text):
    req = BatchSubmitRequest(csv_text=text)
    assert req.collect_urls() == ["https://a.test", "https://b.test", "https://c.test"]


def test_collect_merges_list_and_csv():
    req = BatchSubmitRequest(urls=["https://a.test"], csv_text="https://a.test\nhttps://z.test")
    assert req.collect_urls() == ["https://a.test", "https://z.test"]


# ------------------------------------------------------------- request model
@pytest.mark.parametrize("fmt", ["PNG", "jpeg", "jpg", "webp", "pdf"])
def test_format_is_normalised(fmt):
    assert BatchSubmitRequest(urls=["https://a.test"], format=fmt).format == fmt.lower()


def test_bad_format_rejected():
    with pytest.raises(ValidationError):
        BatchSubmitRequest(urls=["https://a.test"], format="bmp")


@pytest.mark.parametrize("field, value", [
    ("width", 319), ("width", 3841), ("height", 239), ("height", 2161),
    ("delay", -1), ("delay", 11), ("quality", 0), ("quality", 101),
])
def test_bounds(field, value):
    with pytest.raises(ValidationError):
        BatchSubmitRequest(urls=["https://a.test"], **{field: value})


# ------------------------------------------------------- remove_elements
def test_remove_elements_are_cleaned_not_rejected():
    # Documented behaviour: bad entries are dropped or capped, never a 4xx.
    assert batch._validate_remove_elements(None) is None
    assert batch._validate_remove_elements("not-a-list") is None
    assert batch._validate_remove_elements([" .a ", "", 7, "#b"]) == [".a", "#b"]
    assert batch._validate_remove_elements(["", "  "]) is None


def test_remove_elements_capped_at_20_selectors_of_200_chars():
    many = batch._validate_remove_elements([f".c{i}" for i in range(50)])
    assert len(many) == 20
    [long_one] = batch._validate_remove_elements(["." + "x" * 500])
    assert len(long_one) == 200


@pytest.mark.parametrize("raw, expected", [
    (None, None),
    ("", None),
    (".cookie, #popup", [".cookie", "#popup"]),
    ('[".a", "#b"]', [".a", "#b"]),
])
def test_remove_elements_form_parsing(raw, expected):
    assert batch._parse_remove_elements_form(raw) == expected


# ------------------------------------------------------------- job counting
def test_calc_counts_treats_cancelled_as_failed():
    items = [{"status": s} for s in
             ("completed", "completed", "failed", "cancelled", "queued", "processing")]
    assert batch._calc_counts(items) == {
        "completed": 2, "failed": 2, "queued": 1, "processing": 1, "total": 6,
    }


# --------------------------------------------------------- friendly errors
@pytest.mark.parametrize("raw", [
    "Cannot switch to a different thread",
    "Browser.new_context: Target page, context or browser has been closed",
    "Capture abandoned: the capture engine was restarted.",
    "Page.goto: Connection closed while reading from the driver",
    "The capture engine is busy: this capture waited 300 seconds without starting.",
])
def test_engine_errors_are_not_shown_raw(raw):
    msg = batch._friendly_error(raw)
    assert "greenlet" not in msg.lower()
    assert msg != raw
    assert "Retry Failed" in msg


def test_friendly_error_never_empty():
    assert batch._friendly_error("")
    assert batch._friendly_error("something nobody anticipated")


def test_page_timeout_is_explained_without_blaming_the_engine():
    msg = batch._friendly_error("The page did not finish rendering within 180 seconds, so "
                                "the capture was stopped.")
    assert "did not finish rendering in time" in msg
    assert "Retry Failed" not in msg
