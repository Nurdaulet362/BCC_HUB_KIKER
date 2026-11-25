import tempfile
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import Mock
from io import BytesIO
from urllib.error import HTTPError

from pypdf import PdfWriter
from pypdf.generic import (
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
)

from kiker.acceptance import (
    ExpensePeriod,
    compare_expense_period,
    expense_month_rule,
    matches_expense_exclusion,
)
from kiker.audit import Audit, Finding, LOCAL, _ask, _grounded_design_detail, review
from kiker.gemini import Gemini
from kiker.ollama import FormatUnavailable, Ollama, Settings
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

    def test_report_fingerprint_identifies_the_actual_input(self):
        file = source_file(self.directory)
        original = file.read_bytes()
        document = read_document(file)
        file.write_text(MARKDOWN.replace('новый номер', 'другой номер'), encoding='utf-8')
        revised = read_document(file)
        self.assertEqual(document.sha256, hashlib.sha256(original).hexdigest())
        self.assertNotEqual(document.sha256, revised.sha256)
        run = Run(datetime.now(timezone.utc), 'test', 'test', None, 0, 0, 0, 0)
        report = render(document, Audit(document.blocks, ()), run)
        self.assertIn(f'SHA-256 входного файла — `{document.sha256}`', report)

    def test_local_batch_sees_rules_from_the_rest_of_document(self):
        document = read_document(source_file(self.directory))
        reviewer = Mock()
        reviewer.chat.return_value = {'requirements': ['S001'], 'issues': []}
        _ask(reviewer, LOCAL, [document.blocks[0]], local=True, context=list(document.blocks))
        _, content, schema, _ = reviewer.chat.call_args.args
        self.assertIn(document.blocks[1].text, content)
        self.assertEqual(schema['properties']['requirements']['items']['enum'], ['S001'])

    def test_environment_address_gap_needs_a_behaviour_requirement(self):
        document = read_document(source_file(self.directory,
            '# API\n\n| **Request URL** | Dev: /limit. Prod: — |\n\n'
            'REQ-1. Приложение обязано использовать указанный промышленный URL для приёмки.\n'))
        source = {block.id: block for block in document.blocks}
        unsupported = Finding('gap', 'major', 'Нет адреса среды.', 'Какой URL в среде Prod?',
                              ('S001',), 'Отдельное требование')
        self.assertFalse(_grounded_design_detail(unsupported, source))
        required = Finding('gap', 'major', unsupported.description, unsupported.question,
                           ('S001', 'S002'), 'Полнота')
        self.assertTrue(_grounded_design_detail(required, source))

    def test_blank_pdf_needs_text_layer(self):
        file = pdf_file(self.directory / "blank.pdf")
        with self.assertRaisesRegex(DocumentError, "извлекаемого текста"):
            read_document(file)

    def test_empty_markdown_is_an_error(self):
        with self.assertRaises(DocumentError):
            read_document(source_file(self.directory, "# Заголовок\n\n---\n"))

    def test_omits_unknown_id_and_retains_explicit_ids(self):
        document = read_document(source_file(self.directory))
        reviewer = Responses(
            {"requirements": ["S001"], "issues": []},
            {"issues": [issue(sources=["S001", "S999"]), issue()]},
            {"issues": [issue(sources=["S002", "S001"])]},
            {"issues": []},
            {"issues": []},
            {"confirmed": [{"id": "C001", "impact": "При повторе правила требуют разных номеров."}]},
        )
        audit = review(document, reviewer, lambda _: None)
        self.assertEqual([item.original_id for item in audit.requirements], ["R-17", "R-19"])
        self.assertEqual(len(audit.findings), 1)
        self.assertEqual(audit.findings[0].sources, ("S001", "S002"))
        self.assertEqual(audit.rejected, 1)

    def test_single_citation_conflict_never_reaches_report(self):
        document = read_document(source_file(self.directory))
        refusal = {"issues": [issue(sources=["S001"])]}
        reviewer = Responses(
            {"requirements": [], "issues": []}, refusal,
            {"issues": []}, {"issues": []}, {"issues": []},
        )
        result = review(document, reviewer, lambda _: None)
        self.assertEqual(result.findings, ())
        self.assertEqual(result.rejected, 1)

    def test_omits_discretionary_ui_and_sort_order_from_report(self):
        document = read_document(source_file(
            self.directory,
            "# Выгрузка\n\n"
            "REQ-1. При ошибке дат клиент видит сообщение, файл не формируется.\n"
            "REQ-2. CSV включает все операции за указанный период.\n",
        ))
        ui = {
            **issue("gap", ["S001"]),
            "description": "Не указано, отключать ли кнопку выгрузки при ошибке дат.",
            "question": "Должна ли кнопка быть отключена?",
        }
        ordering = {
            **issue("gap", ["S002"]),
            "description": "Не указан порядок сортировки операций в файле.",
            "question": "Как сортировать строки файла?",
        }
        empty_period = {
            **issue("gap", ["S002"]),
            "description": "Нет отдельного правила для периода без операций.",
            "question": "Что вернуть, если операций нет?",
        }
        reviewer = Responses(
            {"requirements": ["S001", "S002"], "issues": []},
            {"issues": []}, {"issues": [ui, ordering, empty_period]}, {"issues": []}, {"issues": []},
            {"confirmed": []},
        )
        audit = review(document, reviewer, lambda _: None)
        self.assertEqual(audit.requirements, document.blocks)
        self.assertEqual(audit.findings, ())
        self.assertEqual(audit.rejected, 0)

    def test_order_finding_requires_evidence_of_order_in_cited_rule(self):
        document = read_document(source_file(
            self.directory,
            "# Выгрузка\n\n"
            "REQ-1. Файл включает все операции за период.\n"
            "REQ-2. Строки сверки должны быть отсортированы по сумме.\n",
        ))
        unsupported = {
            **issue("ambiguous", ["S001"]),
            "severity": "minor",
            "description": "Порядок строк файла не определён.",
            "question": "В каком порядке должны быть отсортированы строки файла?",
        }
        required = {
            **issue("ambiguous", ["S002"]),
            "description": "Направление обязательной сортировки по сумме не задано.",
            "question": "Сортировать строки по возрастанию или по убыванию суммы?",
        }
        reviewer = Responses(
            {"requirements": ["S001", "S002"], "issues": []},
            {"issues": []}, {"issues": [unsupported, required]}, {"issues": []}, {"issues": []},
            {"confirmed": [{"id": "C001", "impact": "Направление изменяет проверку обязательного порядка."}]},
        )
        result = review(document, reviewer, lambda _: None)
        self.assertEqual([finding.sources for finding in result.findings], [("S002",)])

    def test_excluded_operation_checks_the_expected_counter(self):
        rule = "В расходы входят покупки. Переводы и снятие наличных исключаются."
        for action in ("Перевести 300 ₸", "Снять 300 ₸ наличными"):
            for expected, matches in (("12 600 ₸", True), ("12 900 ₸", False)):
                with self.subTest(action=action, expected=expected):
                    row = f"| Расходы 12\u202f600 ₸, порог 15 000 ₸ | {action} | Счётчик показывает {expected} |"
                    self.assertEqual(matches_expense_exclusion(rule, row), matches)

        row = "| Расходы 12 600 ₸ | Перевести 300 ₸ | Счётчик показывает 12 600 ₸ |"
        for other_rule in (
            "В расходы входят покупки. Переводы не исключаются.",
            "В расходы входят покупки. Переводы исключаются при отмене.",
            "В бонусы входят покупки. Переводы исключаются.",
        ):
            with self.subTest(rule=other_rule):
                self.assertFalse(matches_expense_exclusion(other_rule, row))

    def test_reports_only_the_acceptance_row_with_an_incorrect_total(self):
        document = read_document(source_file(
            self.directory,
            "# Учёт расходов\n\n"
            "REQ-1. В расходы входят покупки. Переводы исключаются.\n\n"
            "## Критерии приёмки\n\n"
            "| Расходы 12 600 ₸ | Перевести 300 ₸ | Счётчик показывает 12 600 ₸ |\n"
            "| Расходы 12 600 ₸ | Перевести 300 ₸ | Счётчик показывает 12 900 ₸ |\n",
        ))
        reviewer = Responses(
            {"requirements": ["S001"], "issues": []},
            {"issues": []}, {"issues": []},
            {"issues": [issue(sources=["S001", "S002"]), issue(sources=["S001", "S003"])]},
            {"issues": []},
            {"confirmed": [{"id": "C001", "impact": "Ожидаемый счётчик включает исключённую операцию."}]},
        )
        result = review(document, reviewer, lambda _: None)
        self.assertEqual([finding.sources for finding in result.findings], [("S001", "S003")])

    def test_purchase_month_depends_on_the_explicit_rule(self):
        row = (
            "| Покупка 31 декабря в 23:50 на 6 100 ₸ "
            "| Провести 1 января в 00:05 по Алматы "
            "| Январский счётчик вырос на 6 100 ₸ |"
        )
        purchase_rule = expense_month_rule("Месяц определяется по дате совершения покупки в Алматы.")
        posting_rule = expense_month_rule("Месяц определяется по дате проведения покупки в Алматы.")
        self.assertEqual(
            compare_expense_period(purchase_rule, row),
            ExpensePeriod("31 декабря", "1 января", "декабрь", "январь"),
        )
        self.assertEqual(
            compare_expense_period(posting_rule, row),
            ExpensePeriod("31 декабря", "1 января", "январь", "январь"),
        )
        self.assertIsNone(compare_expense_period(purchase_rule, row.replace("по Алматы", "по Астане")))
        self.assertIsNone(compare_expense_period(purchase_rule, row.replace("31 декабря", "32 декабря")))
        self.assertIsNone(compare_expense_period(purchase_rule, row.replace("вырос на 6 100", "вырос на 7 100")))

    def test_reports_explicit_month_conflict_even_if_model_misses_it(self):
        document = read_document(source_file(
            self.directory,
            "# Учёт расходов\n\n"
            "REQ-1. Месяц определяется по дате совершения покупки.\n\n"
            "## Критерии приёмки\n\n"
            "| Покупка 30 апреля в 23:50 на 6 100 ₸ | Провести 1 мая в 00:05 | Майский счётчик вырос на 6 100 ₸ |\n"
            "| Покупка 30 апреля в 23:50 на 6 100 ₸ | Провести 1 мая в 00:05 | Апрельский счётчик вырос на 6 100 ₸ |\n",
        ))
        reviewer = Responses(
            {"requirements": ["S001"], "issues": []},
            {"issues": []}, {"issues": []},
            {"issues": [issue(sources=["S001", "S003"])]}, {"issues": []},
        )
        audit = review(document, reviewer, lambda _: None)
        self.assertEqual([(f.kind, f.severity, f.sources) for f in audit.findings],
                         [("conflict", "critical", ("S001", "S002"))])
        self.assertIn("месяц расходов — апрель", audit.findings[0].description)

    def test_confirmation_knows_the_programmatically_verified_period(self):
        document = read_document(source_file(self.directory,
            '# Расходы\n\nREQ-1. Месяц определяется по дате совершения покупки.\n\n'
            '## Приёмка\n\n'
            '| Покупка 30 апреля на 500 ₸ | Провести 1 мая | Майский счётчик вырос на 500 ₸ |\n'))
        reviewer = Mock()
        reviewer.chat.side_effect = [
            {'requirements': ['S001'], 'issues': [issue('ambiguous', ['S001'])]},
            {'issues': []}, {'issues': []}, {'issues': []}, {'issues': []}, {'confirmed': []},
        ]
        audit = review(document, reviewer, lambda _: None)
        confirmation = reviewer.chat.call_args.args[1]
        self.assertIn('месяц расходов — апрель', confirmation)
        self.assertIn('S001, S002', confirmation)
        self.assertEqual(len(audit.findings), 1)

    def test_schema_fallback_keeps_the_same_contract(self):
        client = Ollama(Settings("http://127.0.0.1:11434", "test", 4096, 30))
        reply = {"message": {"content": '```json\n{"issues":[]}\n```'}}
        client._request = Mock(side_effect=[FormatUnavailable(), reply, reply])
        schema = {"type": "object"}
        self.assertEqual(client.chat("role", "input", schema, 250), {"issues": []})
        self.assertEqual(client.chat("role", "input", schema, 250), {"issues": []})
        requests = [call.args[1] for call in client._request.call_args_list]
        self.assertEqual(requests[0]["format"], schema)
        self.assertNotIn("format", requests[1])
        self.assertNotIn("format", requests[2])
        self.assertIn('{"type":"object"}', requests[1]["messages"][0]["content"])

    def test_gemini_structured_request_and_usage(self):
        with unittest.mock.patch("kiker.gemini.environment", return_value={"GEMINI_API_KEY": "test"}):
            client = Gemini()
        client._generate = Mock(return_value={
            "modelVersion": "version-test",
            "usageMetadata": {"promptTokenCount": 19, "candidatesTokenCount": 7, "thoughtsTokenCount": 3},
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": "private reasoning", "thought": True}, {"text": '{"issues":[]}'}]},
            }],
        })
        schema = {"type": "array", "uniqueItems": True, "items": {"type": "string"}}
        self.assertEqual(client.chat("role", "input", schema, 250), {"issues": []})
        payload = client._generate.call_args.args[0]
        self.assertEqual(payload["systemInstruction"]["parts"], [{"text": "role"}])
        self.assertEqual(payload["generationConfig"]["responseMimeType"], "application/json")
        self.assertNotIn("uniqueItems", payload["generationConfig"]["responseJsonSchema"])
        self.assertEqual(payload['generationConfig']['thinkingConfig'], {'thinkingLevel': 'MEDIUM'})
        self.assertEqual((client.calls, client.input_tokens, client.output_tokens), (1, 19, 10))

    def test_gemini_waits_for_retry_after_and_recovers(self):
        with unittest.mock.patch('kiker.gemini.environment', return_value={'GEMINI_API_KEY': 'test'}):
            client = Gemini()
        limited = HTTPError('https://example.test', 429, 'quota', {'Retry-After': '45'}, None)
        with (
            unittest.mock.patch('kiker.gemini.urlopen', side_effect=[limited, BytesIO(b'{}')]) as request,
            unittest.mock.patch('kiker.gemini.time.sleep') as sleep,
        ):
            self.assertEqual(client._generate({}), {})
        sleep.assert_any_call(45)
        self.assertEqual(request.call_count, 2)

    def test_gemini_rejects_nonfinite_request_interval(self):
        for interval in ('nan', 'inf', '-1', '61'):
            with self.subTest(interval=interval), unittest.mock.patch(
                'kiker.gemini.environment', return_value={
                    'GEMINI_API_KEY': 'test', 'KIKER_REQUEST_INTERVAL': interval,
                },
            ):
                with self.assertRaisesRegex(RuntimeError, 'KIKER_REQUEST_INTERVAL'):
                    Gemini()

    def test_gemini_does_not_retry_an_exhausted_daily_quota(self):
        with unittest.mock.patch('kiker.gemini.environment', return_value={'GEMINI_API_KEY': 'test'}):
            client = Gemini()
        body = {'error': {'details': [{'violations': [{'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]}]}}
        limited = HTTPError('https://example.test', 429, 'quota', {}, BytesIO(json.dumps(body).encode()))
        with unittest.mock.patch('kiker.gemini.urlopen', side_effect=limited) as request:
            with self.assertRaisesRegex(RuntimeError, 'дневной лимит'):
                client._generate({})
        self.assertEqual(request.call_count, 1)

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
