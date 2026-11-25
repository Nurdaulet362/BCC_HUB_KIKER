"""Render validated findings, quoting the original input rather than the model."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from html import escape
import re

from .audit import Audit, Finding, KINDS, SEVERITIES
from .source import Block, Document


@dataclass(frozen=True)
class Run:
    date: datetime
    version: str
    model: str
    digest: str | None
    seconds: float
    requests: int
    input_tokens: int
    output_tokens: int


def _text(value: str) -> str:
    value = escape(" ".join(value.split()), quote=False)
    return re.sub(r"([\\`*_\[\]|])", r"\\\1", value)


def _lead(block: Block) -> str:
    quote = " ".join(block.text.split())
    return _text(quote if len(quote) <= 250 else quote[:249].rstrip() + "…")


def _link(source: str, index: dict[str, Block]) -> str:
    block = index[source]
    return f"[{source} · {block.location}](#{source.lower()})"


def _issue(number: str, item: Finding, index: dict[str, Block]) -> str:
    evidence = ", ".join(_link(source, index) for source in item.sources)
    return "\n".join((
        f'<a id="{number.lower()}"></a>',
        f"### {number} · {KINDS[item.kind]} · {SEVERITIES[item.severity]}",
        "",
        _text(item.description),
        "",
        f"**Источники** — {evidence}.",
        "",
        f"**Вопрос к автору** — {_text(item.question)}",
    ))


def render(document: Document, audit: Audit, run: Run) -> str:
    source_index = {block.id: block for block in document.blocks}
    totals = Counter(item.severity for item in audit.findings)
    issues = [(f"D{number:03}", item) for number, item in enumerate(audit.findings, start=1)]
    covered = defaultdict(list)
    for number, item in issues:
        for source in item.sources:
            covered[source].append(f"[{number}](#{number.lower()})")

    model_title = _text(run.model)
    if run.digest:
        model_title += f" · `{run.digest[:12]}`"
    report = [
        f"# Ревью ТЗ · {_text(document.title)}",
        "",
        "## Паспорт прогона",
        "",
        f"- Входной файл — {_text(document.name)}.",
        f"- Дата и время — {run.date.isoformat(timespec='seconds')}.",
        f"- Модель — {model_title}, Ollama.",
        f"- Версия агента — `{run.version}`.",
        f"- Время — {run.seconds:.1f} с. Запросов — {run.requests}. "
        f"Токенов — {run.input_tokens} входных и {run.output_tokens} выходных.",
    ]
    if document.empty_pages:
        pages = ", ".join(map(str, document.empty_pages))
        report.append(f"- Страницы без извлечённого текста — {pages}. Полнота проверки ограничена.")
    if audit.rejected:
        report.append(f"- Исключено записей модели, не прошедших проверку схемы и ссылок — {audit.rejected}.")
    report += ["", "## Сводка и готовность", ""]

    if not audit.requirements or document.empty_pages:
        verdict = "Недостаточно данных для вывода о готовности всего ТЗ."
    elif totals["critical"]:
        verdict = "Ключевые сценарии требуют уточнения перед реализацией и приёмкой."
    elif totals["major"]:
        verdict = "Перед реализацией затронутых сценариев согласуйте существенные замечания."
    elif audit.rejected:
        verdict = "Часть выводов модели исключена. Перед разработкой сверьте документ с аналитиком."
    else:
        verdict = "Модель не выявила блокирующих или существенных замечаний. ТЗ готово к ручному согласованию перед разработкой."
    report += [
        f"**{verdict}**",
        "",
        f"- Выделено требований — **{len(audit.requirements)}** из {len(document.blocks)} фрагментов.",
        f"- Замечаний — **{len(issues)}**. Блокирующих — **{totals['critical']}**, "
        f"существенных — **{totals['major']}**, уточнений — **{totals['minor']}**.",
    ]
    priority = [(number, item) for number, item in issues if item.severity == "critical"]
    if not priority:
        priority = issues[:3]
    if priority:
        report += ["", "### Что исправить в первую очередь", ""]
        report.extend(
            f"- [{number}](#{number.lower()}) · {_text(item.description)}"
            for number, item in priority
        )

    report += [
        "", "## Карта и проверка отдельных требований", "",
        "Исходные номера сохранены. Для ненумерованных пунктов агент присвоил ID R и сохранил цитату. "
        "Оценка учитывает однозначность, проверяемость, полноту формулировки "
        "и обоснованность проектных ограничений.",
        "",
        "| ID | Исходный ID | Место | Формулировка из ТЗ | Результат проверки |",
        "| --- | --- | --- | --- | --- |",
    ]
    for position, block in enumerate(audit.requirements, start=1):
        location = _link(block.id, source_index)
        result = ", ".join(covered[block.id]) or "Замечаний нет"
        report.append(
            f"| R{position:03} | {_text(block.original_id or '—')} | {location} | {_lead(block)} | {result} |"
        )

    for title, select in (
        ("Дефекты формулировок", lambda item: item.scope == "Отдельное требование"),
        ("Проверка набора требований", lambda item: item.scope != "Отдельное требование"),
    ):
        report += ["", f"## {title}", ""]
        selected = [(number, item) for number, item in issues if select(item)]
        if title == "Проверка набора требований":
            report += [
                "Проверены связи между бизнес-правилами, примерами приёмки и интерфейсами, "
                "согласованность правил и недостающие условия.",
                "",
            ]
        if selected:
            for number, item in selected:
                report += [_issue(number, item, source_index), ""]
        else:
            report.append("Замечаний в этом проходе нет.")

    report += ["", "## Источники замечаний", ""]
    quote_ids = set(covered) | {block.id for block in audit.requirements}
    for block in document.blocks:
        if block.id in quote_ids:
            section = f" · {_text(block.section)}" if block.section else ""
            report += [
                f'<a id="{block.id.lower()}"></a>',
                f"**{block.id} · {block.location}{section}**",
                "",
                f"> {_text(block.text)}",
                "",
            ]
    return "\n".join(report).rstrip() + "\n"
