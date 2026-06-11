"""Tests for document context notes prepended to attachment turns."""

import importlib

import pytest

gateway_run = importlib.import_module("gateway.run")
_build_document_context_note = gateway_run._build_document_context_note


class TestTextDocumentNote:
    @pytest.mark.parametrize("mtype", ["text/plain", "text/markdown", "text/csv"])
    def test_text_note_mentions_included_content_and_path(self, mtype):
        note = _build_document_context_note("notes.txt", "/cache/doc_notes.txt", mtype)
        assert "text document" in note
        assert "notes.txt" in note
        assert "/cache/doc_notes.txt" in note
        assert "included below" in note


class TestBinaryDocumentNote:
    @pytest.mark.parametrize(
        "mtype",
        [
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/octet-stream",
        ],
    )
    def test_binary_note_guides_extraction(self, mtype):
        note = _build_document_context_note("contract.pdf", "/cache/doc_contract.pdf", mtype)
        assert "/cache/doc_contract.pdf" in note
        assert "extract" in note.lower()
        assert "ask the user" not in note.lower()
        assert "paste" in note.lower()

    def test_binary_note_distinct_from_text_note(self):
        text_note = _build_document_context_note("a.txt", "/c/a.txt", "text/plain")
        pdf_note = _build_document_context_note("a.pdf", "/c/a.pdf", "application/pdf")
        assert text_note != pdf_note
        assert "included below" in text_note
        assert "included below" not in pdf_note
