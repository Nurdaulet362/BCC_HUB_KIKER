"""Gemini Developer API client for text review and JSON output."""

import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import ModelError, environment


DEFAULT_MODEL = "gemini-3.1-flash-lite"
API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models/"


def _api_schema(value):
    if isinstance(value, dict):
        return {
            key: _api_schema(item)
            for key, item in value.items()
            if key not in {"uniqueItems", "minLength", "maxLength"}
        }
    if isinstance(value, list):
        return [_api_schema(item) for item in value]
    return value


class Gemini:
    def __init__(self, model: str | None = None):
        values = environment()
        self.key = (values.get("GEMINI_API_KEY") or values.get("GOOGLE_API_KEY") or "").strip()
        self.model = (model or values.get("KIKER_MODEL") or DEFAULT_MODEL).removeprefix("models/")
        if not self.key:
            raise ModelError("Укажите GEMINI_API_KEY в локальном .env. Ключ создаётся в Google AI Studio.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", self.model):
            raise ModelError("Проверьте модель Gemini в KIKER_MODEL.")
        try:
            self.timeout = float(values.get("KIKER_TIMEOUT", "90"))
            self.request_interval = float(values.get("KIKER_REQUEST_INTERVAL", "15"))
        except ValueError as error:
            raise ModelError("Проверьте KIKER_TIMEOUT и KIKER_REQUEST_INTERVAL в конфигурации.") from error
        if not 1 <= self.timeout <= 1800:
            raise ModelError("KIKER_TIMEOUT должен быть от 1 до 1800 секунд.")
        if not 0 <= self.request_interval <= 60:
            raise ModelError("KIKER_REQUEST_INTERVAL должен быть от 0 до 60 секунд.")
        self.thinking_level = values.get("KIKER_THINKING", "MEDIUM" if self.model.startswith("gemini-3.1-flash-lite") else "LOW").upper()
        if self.thinking_level not in {"MINIMAL", "LOW", "MEDIUM", "HIGH"}:
            raise ModelError("KIKER_THINKING должен быть MINIMAL, LOW, MEDIUM или HIGH.")
        self._next_request = 0.0
        self.calls = 0
        self.attempts = 0
        self.request_times = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.version = None

    def _generate(self, payload: dict) -> dict:
        request = Request(
            f"{API_ROOT}{self.model}:generateContent",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        request.add_unredirected_header("x-goog-api-key", self.key)
        for attempt in range(4):
            time.sleep(max(0, self._next_request - time.monotonic()))
            self._next_request = time.monotonic() + self.request_interval
            self.attempts += 1
            request_started = time.monotonic()
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    result = json.load(response)
                    if not isinstance(result, dict):
                        raise ModelError("Gemini вернул нечитаемый ответ.")
                    return result
            except HTTPError as error:
                if error.code == 429:
                    try:
                        details = json.load(error).get("error", {}).get("details", [])
                    except (ValueError, AttributeError):
                        details = []
                    finally:
                        error.close()
                    if not isinstance(details, list):
                        details = []
                    daily = any(
                        "PerDay" in violation.get("quotaId", "")
                        for detail in details if isinstance(detail, dict)
                        for violation in detail.get("violations", []) if isinstance(violation, dict)
                    )
                    if daily:
                        raise ModelError(
                            f"Исчерпан дневной лимит Gemini для {self.model}. "
                            "Дождитесь сброса квоты или выберите доступную модель через --model."
                        ) from error
                if error.code in {429, 500, 502, 503, 504} and attempt < 3:
                    retry_after = error.headers.get("Retry-After", "")
                    delay = min(60, int(retry_after)) if retry_after.isdigit() else (
                        20 * (attempt + 1) if error.code == 429 else 2 ** (attempt + 1)
                    )
                    time.sleep(delay)
                    continue
                messages = {
                    400: "Gemini отклонил параметры запроса. Проверьте выбранную модель, регион и схему.",
                    401: "Gemini отклонил ключ API.",
                    403: "Проверьте доступ ключа к Gemini Developer API.",
                    404: f"Gemini не нашёл модель {self.model}. Проверьте KIKER_MODEL.",
                    429: "Квота Gemini исчерпана. Проверьте лимиты проекта в Google AI Studio.",
                }
                raise ModelError(messages.get(error.code, f"Gemini вернул HTTP {error.code}.")) from error
            except (URLError, TimeoutError) as error:
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise ModelError("Не удалось подключиться к Gemini API.") from error
            except (UnicodeError, json.JSONDecodeError) as error:
                raise ModelError("Gemini вернул нечитаемый ответ.") from error
            finally:
                self.request_times.append(round(time.monotonic() - request_started, 3))
        raise ModelError("Запрос к Gemini не завершился.")

    def chat(self, system: str, content: str, schema: dict, output_limit: int) -> object:
        config = {
            "temperature": 0,
            "maxOutputTokens": output_limit + 8192,
            "responseMimeType": "application/json",
            "responseJsonSchema": _api_schema(schema),
        }
        if self.model.startswith("gemini-3"):
            config["thinkingConfig"] = {"thinkingLevel": self.thinking_level}
        if self.model.startswith("gemini-2.5-flash"):
            config["thinkingConfig"] = {"thinkingBudget": 0}
        result = self._generate({
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": content}]}],
            "generationConfig": config,
        })
        self.calls += 1
        usage = result.get("usageMetadata", {})
        self.input_tokens += usage.get("promptTokenCount", 0)
        self.output_tokens += usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
        self.version = result.get("modelVersion")
        candidates = result.get("candidates", [])
        if not candidates:
            raise ModelError("Gemini не вернул результат. Проверьте состояние модели и доступ к запросу.")
        candidate = candidates[0]
        reason = candidate.get("finishReason")
        if reason == "MAX_TOKENS":
            raise ModelError("Gemini исчерпал лимит ответа.")
        if reason not in {None, "STOP"}:
            raise ModelError(f"Gemini завершил запрос со статусом {reason}.")
        text = "".join(part.get("text", "") for part in candidate.get("content", {}).get("parts", [])
                       if not part.get("thought"))
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError("Gemini вернул неполный или некорректный JSON") from error
