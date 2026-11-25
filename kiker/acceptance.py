"""Check explicit expense totals and accounting periods in acceptance tables."""

from calendar import monthrange
from dataclasses import dataclass
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
MONTHS = (
    ("январь", "января", "январский"), ("февраль", "февраля", "февральский"),
    ("март", "марта", "мартовский"), ("апрель", "апреля", "апрельский"),
    ("май", "мая", "майский"), ("июнь", "июня", "июньский"),
    ("июль", "июля", "июльский"), ("август", "августа", "августовский"),
    ("сентябрь", "сентября", "сентябрьский"), ("октябрь", "октября", "октябрьский"),
    ("ноябрь", "ноября", "ноябрьский"), ("декабрь", "декабря", "декабрьский"),
)
MONTH_NUMBER = {form: number for number, forms in enumerate(MONTHS, start=1) for form in forms}
MONTH_IN_DATE = "|".join(forms[1] for forms in MONTHS)
MONTH_ADJECTIVE = "|".join(forms[2] for forms in MONTHS)
TIME = r"(?:[01]?\d|2[0-3]):[0-5]\d"
DATE = rf"(?P<day>\d{{1,2}})\s+(?P<month>{MONTH_IN_DATE})(?:\s+в\s+{TIME})?"
MONTH_RULE = re.compile(
    r"месяц(?:\s+(?:уч[её]та\s+)?расходов)?\s+определяется\s+по\s+дате\s+"
    r"(?P<basis>совершения|проведения)\s+покупки(?:\s+в\s+(?P<place>[\w-]+))?",
    re.I,
)
PURCHASE_DATE = re.compile(
    rf"покупка(?:\s+совершена)?\s+{DATE}(?:\s+на\s+{MONEY})?\.?",
    re.I,
)
POSTING_DATE = re.compile(
    rf"провести(?:\s+покупку)?\s+{DATE}(?:\s+по\s+(?P<place>[\w-]+))?\.?",
    re.I,
)
COUNTER_MONTH = re.compile(
    rf"(?P<month>{MONTH_ADJECTIVE})\s+сч[её]тчик(?:\s+расходов)?\s+"
    rf"(?:вырос|увеличился|увеличивается)\s+на\s+{MONEY}\.?",
    re.I,
)


@dataclass(frozen=True)
class ExpensePeriod:
    purchase: str
    posting: str
    rule_month: str
    expected_month: str


def _acceptance_cells(row: str) -> list[str]:
    if row.startswith("|") and row.endswith("|"):
        return [cell.strip() for cell in row[1:-1].split("|")]
    return []


def _money(match: re.Match) -> tuple[Decimal, str]:
    amount = Decimal("".join(match["amount"].split()).replace(",", "."))
    currency = match["currency"].upper().replace("₸", "KZT").replace("₽", "RUB")
    return amount, currency


def matches_expense_exclusion(rule: str, row: str) -> bool:
    """Prove equality only for a simple, explicitly excluded action."""
    cells = _acceptance_cells(row)
    if len(cells) != 3 or not EXPENSES.search(rule):
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


def _date(match: re.Match) -> tuple[int, str] | None:
    month = MONTH_NUMBER[match["month"].lower()]
    day = int(match["day"])
    if 1 <= day <= monthrange(2000, month)[1]:
        return month, f"{day} {MONTHS[month - 1][1]}"
    return None


def expense_month_rule(text: str) -> tuple[str, str] | None:
    """Read an explicit purchase-month rule and its optional local time basis."""
    rules = {
        (rule["basis"].lower(), (rule["place"] or "").casefold())
        for sentence in re.split(r"[.!?]\s*", text)
        if (rule := MONTH_RULE.fullmatch(" ".join(sentence.split()).strip("* ")))
    }
    return rules.pop() if len(rules) == 1 else None


def compare_expense_period(rule: tuple[str, str], row: str) -> ExpensePeriod | None:
    cells = _acceptance_cells(row)
    if len(cells) != 3:
        return None
    before, action, expected = (" ".join(cell.split()) for cell in cells)
    purchase = PURCHASE_DATE.fullmatch(before)
    posting = POSTING_DATE.fullmatch(action)
    counter = COUNTER_MONTH.fullmatch(expected)
    if not purchase or not posting or not counter:
        return None
    if purchase["amount"] and _money(purchase) != _money(counter):
        return None

    basis, place = rule
    if posting["place"] and posting["place"].casefold() != place:
        return None
    purchase_date, posting_date = _date(purchase), _date(posting)
    if purchase_date is None or posting_date is None:
        return None
    rule_month = purchase_date[0] if basis == "совершения" else posting_date[0]
    expected_month = MONTH_NUMBER[counter["month"].lower()]
    return ExpensePeriod(
        purchase_date[1],
        posting_date[1],
        MONTHS[rule_month - 1][0],
        MONTHS[expected_month - 1][0],
    )
