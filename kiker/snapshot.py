"""Portable review records and conservative matching between document versions."""

from dataclasses import asdict
from difflib import SequenceMatcher
import re

from .audit import Audit, KINDS, SEVERITIES
from .report import Run, render
from .source import Document


def snapshot(document: Document, audit: Audit, run: Run) -> dict:
    findings = []
    for number, finding in enumerate(audit.findings, 1):
        findings.append({"id": f"D{number:03}", **asdict(finding)})
    return {
        "schema_version": 1,
        "document": asdict(document),
        "requirements": [block.id for block in audit.requirements],
        "findings": findings,
        "rejected": audit.rejected,
        "run": {**asdict(run), "date": run.date.isoformat(timespec="seconds")},
        "report": render(document, audit, run),
    }


def validate_snapshot(value: object) -> dict:
    """Imported snapshots are untrusted data, never executable markup or paths."""
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Нужен JSON-отчёт KIKER версии 1.")
    document, run = value.get("document"), value.get("run")
    if not isinstance(document, dict) or not isinstance(run, dict):
        raise ValueError("В отчёте нет документа или паспорта прогона.")
    for obj, fields in ((document, ("name", "title", "sha256")),
                        (run, ("model", "version", "date", "provider"))):
        if any(not isinstance(obj.get(key), str) or len(obj[key]) > 2000 for key in fields):
            raise ValueError("Повреждён паспорт JSON-отчёта.")
    if not re.fullmatch(r"[0-9a-f]{64}", document["sha256"]):
        raise ValueError("В JSON-отчёте нужен SHA-256 исходного документа.")
    blocks, findings = document.get("blocks"), value.get("findings")
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 5000:
        raise ValueError("Некорректные исходные фрагменты.")
    if not isinstance(findings, list) or len(findings) > 500:
        raise ValueError("Некорректный список замечаний.")
    source_ids = set()
    for block in blocks:
        if not isinstance(block, dict) or not re.fullmatch(r"S\d{3,}", str(block.get("id", ""))):
            raise ValueError("Некорректный ID исходного фрагмента.")
        if block["id"] in source_ids:
            raise ValueError("Повтор ID исходного фрагмента.")
        source_ids.add(block["id"])
        if any(not isinstance(block.get(k), str) for k in ("text", "section")):
            raise ValueError("В исходном фрагменте нет текста.")
        if any(type(block.get(k)) is not int or block[k] < 1 for k in ("start", "end")):
            raise ValueError("Некорректные номера строк.")
        if block["end"] < block["start"] or (block.get("page") is not None
                and (type(block["page"]) is not int or block["page"] < 1)):
            raise ValueError("Некорректное место цитаты.")
    ids = set()
    for finding in findings:
        if not isinstance(finding, dict) or not re.fullmatch(r"D\d{3,}", str(finding.get("id", ""))):
            raise ValueError("Некорректный ID замечания.")
        if finding["id"] in ids or finding.get("kind") not in KINDS or finding.get("severity") not in SEVERITIES:
            raise ValueError("Некорректный вид или ID замечания.")
        ids.add(finding["id"])
        if any(not isinstance(finding.get(k), str) for k in ("description", "question", "scope")):
            raise ValueError("В замечании нет описания или вопроса.")
        refs = finding.get("sources")
        if not isinstance(refs, list) or not refs or any(not isinstance(s, str) or s not in source_ids for s in refs):
            raise ValueError("Замечание ссылается на отсутствующий источник.")
        if len(set(refs)) != len(refs) or (finding["kind"] == "conflict" and len(refs) < 2):
            raise ValueError("Для противоречия нужны разные источники.")
    requirements = value.get("requirements")
    if not isinstance(requirements, list) or any(not isinstance(s, str) or s not in source_ids for s in requirements):
        raise ValueError("Некорректная карта требований.")
    if not isinstance(value.get("report"), str) or type(value.get("rejected")) is not int:
        raise ValueError("В JSON-отчёте нет текста отчёта или счётчика ошибок.")
    for field in ("seconds", "requests", "input_tokens", "output_tokens"):
        if type(run.get(field)) not in (int, float) or not 0 <= run[field] < 10**12:
            raise ValueError("Некорректные измерения прогона.")
    return value


def _words(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold().replace("ё", "е")))


def compare(before: dict, after: dict) -> dict:
    """Match evidence and wording; an absent finding is not proof of a fix."""
    def evidence(record, finding):
        sources = {block["id"]: block for block in record["document"]["blocks"]}
        return [(sources[s].get("original_id"), _words(sources[s]["text"])) for s in finding["sources"]]

    candidates = []
    for old in before["findings"]:
        for new in after["findings"]:
            if old["kind"] != new["kind"]:
                continue
            a, b = evidence(before, old), evidence(after, new)
            shared = sum(any((aid and aid == bid) or SequenceMatcher(None, at, bt).ratio() >= .78
                             for bid, bt in b) for aid, at in a) / max(len(a), len(b))
            wording = SequenceMatcher(None, _words(old["description"] + old["question"]),
                                      _words(new["description"] + new["question"])).ratio()
            if shared >= .5 and wording >= .55:
                candidates.append((shared + wording, old["id"], new["id"]))
    matched, used_before, used_after = [], set(), set()
    for _, old, new in sorted(candidates, reverse=True):
        if old not in used_before and new not in used_after:
            matched.append({"before": old, "after": new})
            used_before.add(old)
            used_after.add(new)
    return {
        "matched": matched,
        "not_repeated": [f["id"] for f in before["findings"] if f["id"] not in used_before],
        "new": [f["id"] for f in after["findings"] if f["id"] not in used_after],
        "same_input": before["document"]["sha256"] == after["document"]["sha256"],
    }
