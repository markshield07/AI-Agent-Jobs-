from __future__ import annotations

import pytest

from jobagent.resume.extract import (
    EmptyResume,
    UnsupportedResume,
    content_sha,
    extract_text,
)


def test_reads_plain_text():
    text = extract_text(b"Mark Shield\n\n\n\nSenior Engineer\n", "resume.txt")
    assert text == "Mark Shield\n\nSenior Engineer"


def test_markdown_is_read_as_text():
    assert "Engineer" in extract_text(b"# Mark\n\nEngineer", "resume.md")


def test_rejects_unknown_format():
    with pytest.raises(UnsupportedResume):
        extract_text(b"data", "resume.pages")


def test_rejects_a_file_with_no_text():
    with pytest.raises(EmptyResume, match="scanned PDF"):
        extract_text(b"   \n\n  ", "resume.txt")


def test_hash_is_stable_and_content_addressed():
    assert content_sha(b"abc") == content_sha(b"abc")
    assert content_sha(b"abc") != content_sha(b"abd")
