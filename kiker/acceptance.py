"""Check explicitly unchanged expense totals in acceptance table rows."""

from decimal import Decimal
import re


AMOUNT = r"(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?"
CURRENCY = r"(?:₸|₽|KZT|RUB)"
MONEY = rf"(?P<amount>{AMOUNT})\s*(?P<currency>{CURRENCY})"
BEFORE = re.compile(rf"(?:текущие\s+)?расходы\s+{MONEY}(?:,\s*порог\s+{AMOUNT}\s*{CURRENCY})?\.?", re.I)
AFTER = re.compile(rf"(?:сч[её]тчик(?: расходов)?|расходы)\s+(?:показывает|показывают|всё ещё)\s+{MONEY}\.?", re.I)
EXPENSES = re.compile(r"\bрасход(?:ы|ов|ах)\b", re.I)
OPERATIONS = {
    "переводы": re.compile(rf"перевести\s+{MONEY}\.?", re.I),
    "снятие наличных": re.compile(rf"снять\s+(?:наличные\s+на\s+сумму\s+)?{MONEY}(?:\s+наличными)?\.?", re.I),
}
OPERATION = r"(?:переводы|снятие\s+наличных)"
EXCLUSION = re.compile(
    rf"(?P<operations>{OPERATION}(?:\s+и\s+{OPERATION})?)\s+"
    r"(?:исключа(?:ются|ется)(?:\s+из\s+(?:суммы\s+)?расходов)?"
    r"|не\s+учитыва(?:ются|ется)\s+в\s+расходах|не\s+входят\s+в\s+расходы)",
    re.I,
)


def _money(match: re.Match) -> tuple[Decimal, str]:
    amount = Decimal("".join(match["amount"].split()).replace(",", "."))
    currency = match["currency"].upper().replace("₸", "KZT").replace("₽", "RUB")
    return amount, currency


def matches_expense_exclusion(rule: str, row: str) -> bool:
    """Prove equality only for a simple, explicitly excluded action."""
    if not row.startswith("|") or not row.endswith("|") or not EXPENSES.search(rule):
        return False
    cells = [cell.strip() for cell in row[1:-1].split("|")]
    if len(cells) != 3:
        return False
    before, action, expected = cells
    start, end = BEFORE.fullmatch(before), AFTER.fullmatch(expected)
    if start is None or end is None or _money(start) != _money(end):
        return False

    for sentence in re.split(r"[.!?]\s*", rule):
        exclusion = EXCLUSION.fullmatch(" ".join(sentence.split()))
        if exclusion:
            excluded = re.split(r"\s+и\s+", exclusion["operations"].lower())
            if any(OPERATIONS[name].fullmatch(action) for name in excluded):
                return True
    return False
