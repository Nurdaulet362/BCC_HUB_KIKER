"""Run with `python -m kiker INPUT OUTPUT`."""

import argparse
from datetime import datetime
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from .audit import review
from .config import ModelError, environment
from .gemini import Gemini
from .ollama import Ollama, Settings
from .report import Run, render
from .source import read_document


def _version() -> str:
    root = Path(__file__).resolve().parent.parent
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root,
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip()
        if re.fullmatch("[0-9a-f]{40,64}", commit):
            modified = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no", "--", "kiker", "requirements.txt"],
                cwd=root, capture_output=True, text=True, check=True, timeout=5,
            ).stdout.strip()
            return commit + (" (изменённая рабочая копия)" if modified else "")
    except (OSError, subprocess.SubprocessError):
        pass
    return "v1.0.0 (архив исходников)"


def _write_report(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False, prefix=".kiker-",
    ) as handle:
        temp = Path(handle.name)
        try:
            handle.write(text)
            handle.close()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="KIKER · проверка требований до разработки")
    parser.add_argument("input", type=Path, help="входной файл Markdown или PDF")
    parser.add_argument("output", type=Path, help="путь к Markdown-отчёту")
    parser.add_argument("--model", help="модель выбранного провайдера, переопределяет KIKER_MODEL")
    args = parser.parse_args()

    try:
        if args.input.resolve() == args.output.resolve() or (
            args.output.exists() and args.input.exists() and args.input.samefile(args.output)
        ):
            raise ValueError("Для входного документа и отчёта нужны разные файлы.")
        if args.output.suffix.lower() not in {".md", ".markdown"}:
            raise ValueError("Укажите путь к отчёту с расширением .md или .markdown.")
        document = read_document(args.input)
        start = time.monotonic()
        match environment().get("KIKER_PROVIDER", "gemini"):
            case "gemini":
                client = Gemini(args.model)
                provider, model, digest = "Gemini API", client.model, None
            case "ollama":
                settings = Settings.load(args.model)
                client = Ollama(settings)
                provider, model, digest = "Ollama", settings.model, client.model_digest()
            case _:
                raise ModelError("KIKER_PROVIDER должен быть gemini или ollama.")
        print(f"Документ · {document.title} · фрагментов {len(document.blocks)}", file=sys.stderr, flush=True)
        audit = review(document, client, lambda step: print(step, file=sys.stderr, flush=True))
        run = Run(
            datetime.now().astimezone(), _version(), model, digest,
            time.monotonic() - start, client.calls, client.input_tokens, client.output_tokens,
            provider=provider, model_version=client.version if isinstance(client, Gemini) else None,
        )
        _write_report(args.output, render(document, audit, run))
        print(f"Отчёт · {args.output} · требований {len(audit.requirements)} · замечаний {len(audit.findings)}")
        return 0
    except (ValueError, OSError, ModelError) as error:
        print(f"Ошибка · {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
