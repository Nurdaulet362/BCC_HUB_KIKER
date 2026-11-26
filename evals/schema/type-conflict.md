# Тип поля ответа

REQ-1. В каждом успешном JSON-ответе GET /preferences поле enabled обязательно имеет тип integer: 0 — выключено, 1 — включено.
REQ-2. В каждом успешном JSON-ответе GET /preferences поле enabled обязательно имеет тип boolean: false — выключено, true — включено.
REQ-3. Успешное чтение GET /preferences возвращает HTTP 200.
