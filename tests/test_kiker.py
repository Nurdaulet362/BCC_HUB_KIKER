import tempfile
from datetime import datetime, timezone
from pathlib import Path
import unittest

from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)

from kiker.audit import Audit, Finding, review
from kiker.ollama import ModelError
from kiker.report import Run, render
from kiker.source import DocumentError, read_document


MARKDOWN = """# Контрольный отчёт

## Правила
* **R-17.** Повторный запрос возвращает старый номер.
  Повтор определяют по токену операции.
* **R-19.** Повторный запрос всегда выдаёт новый номер.
"""


def source_file(root: Path, content: str = MARKDOWN) -> Path:
    file = root / "input.md"
    file.write_text(content, encoding="utf-8")
    return file


def pdf_file(path: Path, text: bytes = b"") -> Path:
    writer = PdfWriter()
    page = writer.add_blank_page(300, 200)
    if text:
        name = NameObject
        page[name("/Resources")] = DictionaryObject({
            name("/Font"): DictionaryObject({
                name("/F1"): DictionaryObject({
                    name("/Type"): name("/Font"),
                    name("/Subtype"): name("/Type1"),
                    name("/BaseFont"): name("/Helvetica"),
                }),
            }),
        })
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 25 150 Td (" + text + b") Tj ET")
        page[name("/Contents")] = writer._add_object(stream)
    writer.write(path)
    return path


class Responses:
    def __init__(self, *replies):
        self.replies = iter(replies)

    def chat(self, system, content, schema, output_limit):
        return next(self.replies)


def issue(kind="conflict", sources=None):
    return {
        "kind": kind,
        "severity": "critical",
        "sources": ["S001", "S002"] if sources is None else sources,
        "description": "Два правила требуют разных номеров при повторе.",
        "question": "Какой номер возвращать повторно?",
    }


class KikerTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.directory = Path(workspace.name)

    def test_markdown_keeps_ids_and_line_ranges(self):
        document = read_document(source_file(self.directory))
        self.assertEqual(document.title, "Контрольный отчёт")
        self.assertEqual(document.blocks[0].original_id, "R-17")
        self.assertEqual(document.blocks[0].section, "Правила")
        self.assertEqual((document.blocks[0].start, document.blocks[0].end), (4, 5))
        self.assertEqual(document.blocks[1].original_id, "R-19")

    def test_pdf_keeps_page_reference(self):
        file = pdf_file(self.directory / "input.pdf", b"REQ-3. Confirm a transfer.")
        block = read_document(file).blocks[0]
        self.assertEqual(block.original_id, "REQ-3")
        self.assertEqual(block.page, 1)
        self.assertIn("стр. 1", block.location)

    def test_blank_pdf_needs_text_layer(self):
        file = pdf_file(self.directory / "blank.pdf")
        with self.assertRaisesRegex(DocumentError, "извлекаемого текста"):
            read_document(file)

    def test_empty_markdown_is_an_error(self):
        with self.assertRaises(DocumentError):
            read_document(source_file(self.directory, "# Заголовок\n\n---\n"))

    def test_retries_unknown_id_and_retains_explicit_ids(self):
        document = read_document(source_file(self.directory))
        reviewer = Responses(
            {"requirements": ["S001"], "issues": []},
            {"issues": [issue(sources=["S001", "S999"])]},
            {"issues": [issue()]},
            {"issues": [issue(sources=["S002", "S001"])]},
        )
        audit = review(document, reviewer, lambda _: None)
        self.assertEqual([item.original_id for item in audit.requirements], ["R-17", "R-19"])
        self.assertEqual(len(audit.findings), 1)
        self.assertEqual(audit.findings[0].sources, ("S001", "S002"))

    def test_single_citation_conflict_never_reaches_report(self):
        document = read_document(source_file(self.directory))
        refusal = {"issues": [issue(sources=["S001"])]}
        reviewer = Responses({"requirements": [], "issues": []}, refusal, refusal, refusal)
        with self.assertRaisesRegex(ModelError, "двух разных источников"):
            review(document, reviewer, lambda _: None)

    def test_report_uses_source_quotes_and_consistent_counts(self):
        document = read_document(source_file(self.directory))
        finding = Finding(
            "conflict", "critical", "Несовместимые правила.", "Какой номер вернуть?",
            ("S001", "S002"), "Согласованность",
        )
        run = Run(datetime(2026, 11, 25, tzinfo=timezone.utc), "abc", "test", None, 1, 1, 10, 5)
        result = render(document, Audit(document.blocks, (finding,)), run)
        self.assertIn("Выделено требований — **2**", result)
        self.assertIn("Блокирующих — **1**", result)
        self.assertIn("Повтор определяют по токену операции", result)
        self.assertIn('id="s002"', result)
        self.assertIn("[S002 · строки 6](#s002)", result)


if __name__ == "__main__":
    unittest.main()
