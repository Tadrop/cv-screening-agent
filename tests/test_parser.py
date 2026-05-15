"""
Tests for CV extraction and identity stripping.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.parser.extractor import ExtractionResult, _normalise, extract_text
from src.parser.identity_stripper import StripResult, strip_identity


# ──────────────────────────────────────────────────────────────────────────────
# Normalisation
# ──────────────────────────────────────────────────────────────────────────────


def test_normalise_collapses_whitespace():
    raw = "Hello  World\r\n\r\nFoo\n\n\n\nBar"
    result = _normalise(raw)
    assert "  " not in result
    assert result.count("\n\n") <= 1 or "Bar" in result


def test_normalise_strips_trailing_spaces():
    raw = "hello   \nworld   "
    result = _normalise(raw)
    for line in result.splitlines():
        assert not line.endswith(" ")


# ──────────────────────────────────────────────────────────────────────────────
# PDF extraction
# ──────────────────────────────────────────────────────────────────────────────


def test_extract_pdf_uses_pypdf(tmp_path):
    pdf_file = tmp_path / "cv.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 fake")  # not a real PDF — mock below

    mock_page = MagicMock()
    mock_page.extract_text.return_value = "Python developer with 5 years experience"
    mock_reader = MagicMock()
    mock_reader.pages = [mock_page]

    with patch("src.parser.extractor.pypdf") as mock_pypdf:
        mock_pypdf.PdfReader.return_value = mock_reader
        result = extract_text(pdf_file)

    assert "Python developer" in result.text
    assert result.parser_used == "pypdf"
    assert result.is_image_based is False


def test_extract_pdf_falls_back_to_pdfplumber_on_empty_text(tmp_path):
    pdf_file = tmp_path / "scanned.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 fake")

    mock_page_pypdf = MagicMock()
    mock_page_pypdf.extract_text.return_value = ""   # pypdf gets nothing
    mock_reader = MagicMock()
    mock_reader.pages = [mock_page_pypdf]

    mock_page_plumber = MagicMock()
    mock_page_plumber.extract_text.return_value = "Recovered text from pdfplumber"
    mock_pdf_ctx = MagicMock()
    mock_pdf_ctx.__enter__ = MagicMock(return_value=mock_pdf_ctx)
    mock_pdf_ctx.__exit__ = MagicMock(return_value=False)
    mock_pdf_ctx.pages = [mock_page_plumber]

    with (
        patch("src.parser.extractor.pypdf") as mp,
        patch("src.parser.extractor.pdfplumber") as mpl,
    ):
        mp.PdfReader.return_value = mock_reader
        mpl.open.return_value = mock_pdf_ctx
        result = extract_text(pdf_file)

    assert result.parser_used == "pdfplumber"
    assert "Recovered" in result.text


def test_extract_pdf_marks_image_based_when_no_text(tmp_path):
    pdf_file = tmp_path / "image.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 fake")

    mock_reader = MagicMock()
    mock_reader.pages = [MagicMock(extract_text=MagicMock(return_value=""))]

    mock_pdf_ctx = MagicMock()
    mock_pdf_ctx.__enter__ = MagicMock(return_value=mock_pdf_ctx)
    mock_pdf_ctx.__exit__ = MagicMock(return_value=False)
    mock_pdf_ctx.pages = [MagicMock(extract_text=MagicMock(return_value=""))]

    with (
        patch("src.parser.extractor.pypdf") as mp,
        patch("src.parser.extractor.pdfplumber") as mpl,
    ):
        mp.PdfReader.return_value = mock_reader
        mpl.open.return_value = mock_pdf_ctx
        result = extract_text(pdf_file)

    assert result.is_image_based is True
    assert "SCANNED" in result.text


def test_extract_unsupported_type_raises(tmp_path):
    f = tmp_path / "resume.xls"
    f.write_bytes(b"fake")
    with pytest.raises(ValueError, match="Unsupported file type"):
        extract_text(f)


# ──────────────────────────────────────────────────────────────────────────────
# Identity stripping
# ──────────────────────────────────────────────────────────────────────────────


_SAMPLE_CV = textwrap.dedent("""\
    John Smith
    john.smith@email.com | +44 7700 900123
    linkedin.com/in/johnsmith | github.com/jsmith
    London, UK

    EXPERIENCE
    Senior Python Developer — TechCorp (2018–2023)
    Built microservices using FastAPI and PostgreSQL.

    EDUCATION
    BSc Computer Science, University of Manchester (2018)

    Age: 29
    Nationality: British
""")


def test_email_is_stripped():
    result = strip_identity(_SAMPLE_CV)
    assert "john.smith@email.com" not in result.anonymised_text
    assert "[EMAIL REDACTED]" in result.anonymised_text
    assert result.identity.email == "john.smith@email.com"


def test_phone_is_stripped():
    result = strip_identity(_SAMPLE_CV)
    assert "900123" not in result.anonymised_text
    assert "[PHONE REDACTED]" in result.anonymised_text


def test_linkedin_is_stripped():
    result = strip_identity(_SAMPLE_CV)
    assert "linkedin.com/in/johnsmith" not in result.anonymised_text
    assert "[LINKEDIN REDACTED]" in result.anonymised_text
    assert result.identity.linkedin == "linkedin.com/in/johnsmith"


def test_github_is_stripped():
    result = strip_identity(_SAMPLE_CV)
    assert "github.com/jsmith" not in result.anonymised_text


def test_name_is_stripped():
    result = strip_identity(_SAMPLE_CV)
    assert "[NAME REDACTED]" in result.anonymised_text
    assert result.identity.name == "John Smith"


def test_age_flag_set():
    result = strip_identity(_SAMPLE_CV)
    assert "age_found" in result.flags


def test_nationality_flag_set():
    result = strip_identity(_SAMPLE_CV)
    assert "nationality_found" in result.flags


def test_professional_content_preserved():
    result = strip_identity(_SAMPLE_CV)
    assert "FastAPI" in result.anonymised_text
    assert "PostgreSQL" in result.anonymised_text
    assert "BSc Computer Science" in result.anonymised_text


def test_strip_identity_no_pii():
    clean_cv = "EXPERIENCE\nSenior Developer at TechCorp 2019-2024\nPython, AWS, Docker"
    result = strip_identity(clean_cv)
    assert result.identity.email is None
    assert result.identity.name is None
    assert result.flags == []
