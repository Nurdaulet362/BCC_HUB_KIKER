"""Small Ollama client with structured output and bounded retries."""

import json
from dataclasses import dataclass
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class ModelError(RuntimeError):
    """The model service could not complete a review."""


def _dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    entries = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if separator:
            entries[name.strip()] = value.strip().strip("\"'")
    return entries


@dataclass(frozen=True)
class Settings:
    base_url: str
    model: str
    context: int
    timeout: float

    @classmethod
    def load(cls, model: str | None = None) -> "Settings":
        values = _dotenv(Path(".env")) | os.environ
        base_url = values.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
        url = urlsplit(base_url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.query or url.fragment:
            raise ModelError("OLLAMA_URL должен быть базовым HTTP(S)-адресом без учётных данных.")
        try:
            context = int(values.get("KIKER_CONTEXT", "12288"))
            timeout = float(values.get("KIKER_TIMEOUT", "300"))
        except ValueError as error:
            raise ModelError("Проверьте KIKER_CONTEXT и KIKER_TIMEOUT в конфигурации.") from error
        if not 4096 <= context <= 131072 or not 1 <= timeout <= 1800:
            raise ModelError("KIKER_CONTEXT должен быть от 4096 до 131072, KIKER_TIMEOUT — от 1 до 1800 секунд.")
        model = model or values.get("KIKER_MODEL", "qwen3.5:4b")
        if not model.strip():
            raise ModelError("Укажите название модели в KIKER_MODEL.")
        return cls(base_url, model, context, timeout)


class Ollama:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def _request(self, endpoint: str, data: dict | None = None) -> dict:
        address = self.settings.base_url + endpoint
        request = Request(
            address,
            data=json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None,
            headers={"Content-Type": "application/json"},
        )
        for attempt in range(2):
            try:
                with urlopen(request, timeout=self.settings.timeout) as response:
                    return json.load(response)
            except HTTPError as error:
                if error.code in {429, 502, 503, 504} and attempt == 0:
                    time.sleep(1)
                    continue
                if error.code == 404 and endpoint == "/api/chat":
                    raise ModelError(f"Модель недоступна. Выполните ollama pull {self.settings.model}.") from error
                raise ModelError(f"Ollama вернул HTTP {error.code} для {endpoint}.") from error
            except (URLError, TimeoutError) as error:
                raise ModelError(f"Ollama недоступен по адресу {self.settings.base_url}. Запустите ollama serve.") from error
            except (UnicodeError, json.JSONDecodeError) as error:
                raise ModelError("Ollama вернул нечитаемый ответ.") from error
        raise ModelError("Запрос к Ollama не завершился.")

    def model_digest(self) -> str | None:
        model = self.settings.model
        if ":" not in model.rsplit("/", 1)[-1]:
            model += ":latest"
        tags = self._request("/api/tags").get("models", [])
        return next((tag["digest"] for tag in tags if tag.get("name") == model and tag.get("digest")), None)

    def chat(self, system: str, content: str, schema: dict, output_limit: int) -> object:
        # Bound the request before Ollama can silently drop the oldest source text.
        if len(system) + len(content) > (self.settings.context - output_limit - 512) * 2:
            raise ModelError("Текст не помещается в контекст модели. Увеличьте KIKER_CONTEXT для этой модели.")
        response = self._request("/api/chat", {
            "model": self.settings.model,
            "stream": False,
            "think": False,
            "format": schema,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "options": {
                "temperature": 0,
                "seed": 42,
                "num_ctx": self.settings.context,
                "num_predict": output_limit,
                "presence_penalty": 0,
            },
        })
        self.calls += 1
        self.input_tokens += response.get("prompt_eval_count", 0)
        self.output_tokens += response.get("eval_count", 0)
        if response.get("done_reason") == "length":
            raise ModelError("Модель исчерпала лимит ответа. Попробуйте модель с более стабильным JSON.")
        try:
            return json.loads(response["message"]["content"])
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError("пустой или незавершённый JSON") from error
