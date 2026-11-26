"""Local review workbench. Run with `python -m kiker.web`."""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
from threading import RLock
from urllib.parse import parse_qs, urlsplit
import uuid

from .__main__ import _write_report, run_document
from .config import ModelError
from .snapshot import compare, snapshot, validate_snapshot
from .source import read_document

MAX_BODY = 12 * 1024 * 1024
MAX_FILE = 8 * 1024 * 1024
STATES = {"pending", "accepted", "rejected", "clarify"}
ASSETS = Path(__file__).parent / "static"


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, root: Path, execute=run_document):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.execute = execute
        self.lock = RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kiker-review")
        for path in self.root.glob("*/meta.json"):
            entry = json.loads(path.read_text())
            if entry["status"] in {"queued", "running"}:
                entry.update(status="error", error="Предыдущий запуск был прерван. Запустите проверку снова.")
                self._save(path, entry)

    def close(self):
        self.pool.shutdown(wait=True)

    def directory(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("Некорректный номер проверки.")
        directory = self.root / run_id
        if not (directory / "meta.json").is_file():
            raise FileNotFoundError("Проверка не найдена.")
        return directory

    def _save(self, path, value):
        _write_report(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    def list(self):
        with self.lock:
            entries = [json.loads(p.read_text()) for p in self.root.glob("*/meta.json")]
        return sorted(entries, key=lambda item: item["created_at"], reverse=True)

    def get(self, run_id):
        with self.lock:
            directory = self.directory(run_id)
            entry = json.loads((directory / "meta.json").read_text())
            if entry["status"] == "complete":
                entry["result"] = json.loads((directory / "result.json").read_text())
                entry["decisions"] = json.loads((directory / "decisions.json").read_text())
            return entry

    def _create(self, filename, status, imported=False):
        run_id = uuid.uuid4().hex
        directory = self.root / run_id
        directory.mkdir(mode=0o700)
        entry = {"id": run_id, "name": filename, "title": filename, "status": status,
                 "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                 "step": "Ожидает запуска", "imported": imported}
        self._save(directory / "meta.json", entry)
        return directory, entry

    def submit(self, filename, data):
        if not isinstance(filename, str) or len(filename) > 200 or any(c in filename for c in ('/', '\\', '\x00')):
            raise ValueError("Некорректное имя файла.")
        suffix = Path(filename).suffix.lower()
        if suffix not in {".md", ".markdown", ".pdf"}:
            raise ValueError("Загрузите Markdown или PDF с текстовым слоем.")
        if not data or len(data) > MAX_FILE:
            raise ValueError("Файл пуст или превышает 8 МБ.")
        with self.lock:
            if sum(item["status"] in {"queued", "running"} for item in self.list()) >= 3:
                raise Conflict("Уже выполняются или ожидают три проверки. Дождитесь результата.")
            directory, entry = self._create(filename, "queued")
            path = directory / ("input" + suffix)
            path.write_bytes(data)
            try:
                document = replace(read_document(path), name=filename)
            except (OSError, ValueError):
                # A rejected upload is not a successful run and never reaches the model.
                path.unlink(missing_ok=True)
                (directory / "meta.json").unlink()
                directory.rmdir()
                raise
            entry.update(title=document.title, sha256=document.sha256)
            self._save(directory / "meta.json", entry)
            self.pool.submit(self._run, directory, document)
        return entry

    def _run(self, directory, document):
        def progress(step):
            with self.lock:
                entry = json.loads((directory / "meta.json").read_text())
                entry.update(status="running", step=step)
                self._save(directory / "meta.json", entry)
        try:
            progress("Читаем требования")
            audit, run = self.execute(document, progress=progress)
            self._complete(directory, snapshot(document, audit, run))
        except (OSError, ValueError, ModelError) as error:
            with self.lock:
                entry = json.loads((directory / "meta.json").read_text())
                entry.update(status="error", error=str(error), step="Проверка не завершилась")
                self._save(directory / "meta.json", entry)
        except Exception:
            with self.lock:
                entry = json.loads((directory / "meta.json").read_text())
                entry.update(status="error", error="Не удалось завершить проверку. Повторите запуск.")
                self._save(directory / "meta.json", entry)

    def _complete(self, directory, result):
        with self.lock:
            self._save(directory / "result.json", result)
            self._save(directory / "decisions.json", {"revision": 0, "items": {}, "updated_at": None})
            entry = json.loads((directory / "meta.json").read_text())
            entry.update(status="complete", step="Готово", title=result["document"]["title"],
                         sha256=result["document"]["sha256"], findings=len(result["findings"]),
                         seconds=result["run"]["seconds"], model=result["run"]["model"])
            self._save(directory / "meta.json", entry)

    def import_result(self, result):
        result = validate_snapshot(result)
        with self.lock:
            directory, entry = self._create(result["document"]["name"], "queued", imported=True)
            self._complete(directory, result)
        return self.get(entry["id"])

    def decide(self, run_id, payload):
        with self.lock:
            entry = self.get(run_id)
            if entry["status"] != "complete":
                raise Conflict("Дождитесь завершения проверки.")
            state = entry["decisions"]
            if type(payload.get("revision")) is not int or payload["revision"] != state["revision"]:
                raise Conflict("Решения уже изменены в другой вкладке. Обновите страницу.")
            finding_id, decision, note = payload.get("id"), payload.get("decision"), payload.get("note", "")
            if finding_id not in {f["id"] for f in entry["result"]["findings"]} or decision not in STATES:
                raise ValueError("Выберите замечание и решение.")
            if not isinstance(note, str) or len(note) > 2000:
                raise ValueError("Комментарий должен быть короче 2000 символов.")
            state["items"][finding_id] = {"decision": decision, "note": note}
            state["revision"] += 1
            state["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            self._save(self.directory(run_id) / "decisions.json", state)
            return state


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store):
        self.store, self.token = store, secrets.token_urlsafe(32)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, value, status=200, content_type="application/json; charset=utf-8", filename=None):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def _allowed_host(self):
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        return self.headers.get("Host") in allowed

    def do_GET(self):
        if not self._allowed_host():
            return self._send({"error": "Откройте приложение через localhost."}, 403)
        path = urlsplit(self.path)
        try:
            assets = {"/": ("index.html", "text/html"), "/app.css": ("app.css", "text/css"),
                      "/app.js": ("app.js", "text/javascript")}
            if path.path in assets:
                name, mime = assets[path.path]
                return self._send((ASSETS / name).read_bytes(), content_type=mime + "; charset=utf-8")
            if path.path == "/api/session":
                return self._send({"token": self.server.token})
            if path.path == "/api/runs":
                return self._send(self.server.store.list())
            if path.path == "/api/comparison":
                query = parse_qs(path.query)
                before = self.server.store.get(query.get("before", [""])[0])
                after = self.server.store.get(query.get("after", [""])[0])
                if any(item["status"] != "complete" for item in (before, after)):
                    raise Conflict("Сравнивать можно завершённые проверки.")
                return self._send({"before": before, "after": after, **compare(before["result"], after["result"])})
            match = re.fullmatch(r"/api/runs/([a-f0-9]{32})(?:/(report\.md|result\.json|review\.json))?", path.path)
            if match:
                entry = self.server.store.get(match[1])
                if not match[2]:
                    return self._send(entry)
                if entry["status"] != "complete":
                    raise Conflict("Отчёт пока не готов.")
                if match[2] == "report.md":
                    return self._send(entry["result"]["report"].encode(), content_type="text/markdown; charset=utf-8", filename="report.md")
                return self._send(entry["result"] if match[2] == "result.json" else entry,
                                  filename=match[2])
            self._send({"error": "Страница не найдена."}, 404)
        except Conflict as error:
            self._send({"error": str(error)}, 409)
        except (ValueError, FileNotFoundError) as error:
            self._send({"error": str(error)}, 400)

    def do_POST(self):
        expected_origin = f'http://{self.headers.get("Host", "")}'
        if (not self._allowed_host() or self.headers.get("X-Kiker-Token") != self.server.token
                or self.headers.get("Origin", expected_origin) != expected_origin):
            return self._send({"error": "Обновите страницу приложения."}, 403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                return self._send({"error": "Запрос пуст или превышает допустимый размер."}, 413)
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Нужен JSON-объект.")
            path = urlsplit(self.path).path
            if path == "/api/runs":
                encoded = payload.get("data")
                if not isinstance(encoded, str):
                    raise ValueError("Не выбран документ.")
                result = self.server.store.submit(payload.get("name"), base64.b64decode(encoded, validate=True))
                return self._send(result, 202)
            if path == "/api/import":
                return self._send(self.server.store.import_result(payload), 201)
            match = re.fullmatch(r"/api/runs/([a-f0-9]{32})/decisions", path)
            if match:
                return self._send(self.server.store.decide(match[1], payload))
            self._send({"error": "Действие не найдено."}, 404)
        except Conflict as error:
            self._send({"error": str(error)}, 409)
        except (ValueError, OSError) as error:
            self._send({"error": str(error)}, 400)


def main():
    parser = argparse.ArgumentParser(description="KIKER · локальный экран ревью")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=Path(".kiker-runs"))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Порт должен быть от 1 до 65535.")
    store = Store(args.data_dir)
    server = Server(("127.0.0.1", args.port), store)
    print(f"KIKER · http://127.0.0.1:{args.port} · Ctrl+C для остановки", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
