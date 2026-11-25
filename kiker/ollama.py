"""Small Ollama client with structured output and bounded retries."""

import json
from dataclasses import dataclass
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .config import ModelError, environment


class FormatUnavailable(ModelError):
    """The selected runner does not implement native schema constraints."""


@dataclass(frozen=True)
class Settings:
    base_url: str
    model: str
    context: int
    timeout: float

    @classmethod
    def load(cls, model: str | None = None) -> "Settings":
        values = environment()
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
        self.native_schema = True
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
                    body = json.load(response)
                    if not isinstance(body, dict):
                        raise ModelError("Ollama вернул ответ вместо JSON-объекта.")
                    return body
            except HTTPError as error:
                if error.code in {429, 502, 503, 504} and attempt == 0:
                    time.sleep(1)
                    continue
                if error.code == 404 and endpoint == "/api/chat":
                    raise ModelError(f"Модель недоступна. Выполните ollama pull {self.settings.model}.") from error
                if error.code == 501 and data and "format" in data:
                    raise FormatUnavailable("У драйвера нет встроенной поддержки JSON Schema.") from error
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
        request = {
            "model": self.settings.model,
            "stream": False,
            "think": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "options": {
                "temperature": 0,
                "seed": 42,
                "num_ctx": self.settings.context,
                "num_predict": output_limit,
                "presence_penalty": 0,
            },
        }
        # Bound the request before Ollama can silently drop the oldest source text.
        limit = (self.settings.context - output_limit - 512) * 2
        if len(system) + len(content) > limit:
            raise ModelError("Текст не помещается в контекст модели. Увеличьте KIKER_CONTEXT для этой модели.")
        if self.native_schema:
            try:
                response = self._request("/api/chat", {**request, "format": schema})
            except FormatUnavailable:
                self.native_schema = False
        if not self.native_schema:
            system += (
                "\nВерни только один JSON-объект без ограждений Markdown. Схема ответа:\n"
                + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
            )
            if len(system) + len(content) > limit:
                raise ModelError("Исходник и схема не помещаются в контекст модели. Увеличьте KIKER_CONTEXT.")
            request["messages"][0]["content"] = system
            response = self._request("/api/chat", request)

        self.calls += 1
        self.input_tokens += response.get("prompt_eval_count", 0)
        self.output_tokens += response.get("eval_count", 0)
        if response.get("done_reason") == "length":
            raise ModelError("Модель исчерпала лимит ответа. Попробуйте модель с более стабильным JSON.")
        try:
            answer = response["message"]["content"].strip()
            wrapper = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", answer, flags=re.I)
            return json.loads(wrapper.group(1) if wrapper else answer)
        except (AttributeError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError("пустой или незавершённый JSON") from error
