"""Extract citable blocks in document order."""

from dataclasses import dataclass
import hashlib
from io import BytesIO
from pathlib import Path
import re


HEADING = re.compile(r"^(#{1,6})\s+(.+?)(?:\s+#+)?\s*$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
TABLE_RULE = re.compile(r"^[\s|:\-]+$")
ITEM = re.compile(r"^\s*(?:[-*+•]\s+|\d+[.)]\s+)")
DOCUMENT_ID = re.compile(
    r"^\s*(?:[-*+•]\s+)?(?:\*\*|__|`)?"
    r"([A-Za-zА-Яа-яЁё][\w]*[-–]\d+(?:\.\d+)*)"
    r"(?=[.:\s*`_]|$)"
)


class DocumentError(ValueError):
    """The input could not be read as a supported, citable document."""


@dataclass(frozen=True)
class Block:
    id: str
    text: str
    section: str
    start: int
    end: int
    page: int | None = None
    original_id: str | None = None

    @property
    def location(self) -> str:
        lines = str(self.start) if self.start == self.end else f"{self.start}–{self.end}"
        if self.page is None:
            return f"строки {lines}"
        return f"стр. {self.page}, строки извлечения {lines}"


@dataclass(frozen=True)
class Document:
    title: str
    name: str
    blocks: tuple[Block, ...]
    empty_pages: tuple[int, ...] = ()
    sha256: str | None = None


def _extract_blocks(pages: list[tuple[int | None, str]]) -> tuple[Block, ...]:
    blocks: list[Block] = []
    headings: list[tuple[int, str]] = []
    for page, content in pages:
        pending: list[tuple[int, str]] = []
        fence = ""

        def flush() -> None:
            if not pending:
                return
            text = "\n".join(line for _, line in pending).strip()
            if text and any(char.isalnum() for char in text):
                match = DOCUMENT_ID.match(text)
                blocks.append(Block(
                    id=f"S{len(blocks) + 1:03}",
                    text=text,
                    section=" / ".join(title for depth, title in headings if depth > 1),
                    start=pending[0][0],
                    end=pending[-1][0],
                    page=page,
                    original_id=match.group(1) if match else None,
                ))
            pending.clear()

        for number, line in enumerate(content.splitlines(), start=1):
            line = line.strip()
            marker = FENCE.match(line)
            if fence:
                if marker and marker.group(1).startswith(fence):
                    flush()
                    fence = ""
                else:
                    pending.append((number, line))
                continue
            if marker:
                flush()
                fence = marker.group(1)
                continue
            heading = HEADING.match(line)
            if heading:
                flush()
                level = len(heading.group(1))
                headings = [(depth, title) for depth, title in headings if depth < level]
                headings.append((level, heading.group(2)))
                continue
            if not line or RULE.fullmatch(line):
                flush()
                continue
            if line.startswith("|"):
                flush()
                if not TABLE_RULE.fullmatch(line):
                    pending.append((number, line))
                    flush()
                continue
            if ITEM.match(line) or DOCUMENT_ID.match(line):
                flush()
            pending.append((number, line))
        flush()
    return tuple(blocks)


def read_document(path: Path) -> Document:
    untitled = path.stem
    if path.suffix.lower() not in {".md", ".markdown", ".pdf"}:
        raise DocumentError("Поддерживаются текстовые PDF и Markdown (.md, .markdown).")
    data = path.read_bytes()
    match path.suffix.lower():
        case ".md" | ".markdown":
            try:
                content = data.decode("utf-8-sig")
            except UnicodeError as error:
                raise DocumentError("Сохраните Markdown в UTF-8.") from error
            heading = next(
                (m.group(2) for line in content.splitlines()
                 if (m := HEADING.match(line)) and m.group(1) == "#"),
                untitled,
            )
            title, pages = heading, [(None, content)]
        case ".pdf":
            from pypdf import PdfReader
            from pypdf.errors import PyPdfError

            try:
                with BytesIO(data) as file:
                    reader = PdfReader(file)
                    if reader.is_encrypted:
                        raise DocumentError("Передайте PDF без пароля.")
                    title = (reader.metadata.title if reader.metadata else None) or untitled
                    pages = [
                        (number, page.extract_text(extraction_mode="layout"))
                        for number, page in enumerate(reader.pages, start=1)
                    ]
            except PyPdfError as error:
                raise DocumentError("Не удалось прочитать PDF.") from error
        case _:
            raise DocumentError("Поддерживаются текстовые PDF и Markdown (.md, .markdown).")

    blocks = _extract_blocks(pages)
    if not blocks:
        raise DocumentError("В документе нет извлекаемого текста. Для скана нужен OCR.")
    pages_with_text = {block.page for block in blocks}
    empty_pages = tuple(page for page, _ in pages if page is not None and page not in pages_with_text)
    return Document(title, path.name, blocks, empty_pages, hashlib.sha256(data).hexdigest())
