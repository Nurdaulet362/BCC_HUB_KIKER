# Запуск и настройка KIKER

[← К README](../README.md)

Основной путь установки и одна команда анализа приведены в [быстром запуске](../README.md#быстрый-запуск). Все команды ниже выполняются из корня репозитория с активированным виртуальным окружением.

## Установка на Windows

В PowerShell:

```powershell
git clone https://github.com/Nurdaulet362/BCC_HUB_KIKER.git
cd BCC_HUB_KIKER
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Впишите ключ в `GEMINI_API_KEY` в `.env`, затем запустите:

```powershell
.venv\Scripts\python.exe -m kiker examples/requirements.md report.md --json result.json
```

Для интерфейса используйте `.venv\Scripts\python.exe -m kiker.web`. Активация окружения для этих команд не нужна. Версия установленного Python должна быть 3.11–3.14.

## CLI

```sh
python -m kiker examples/requirements.md report.md --json result.json
python -m kiker /path/to/document.pdf report.md --json result.json
python -m kiker --help
```

Первый аргумент — Markdown или PDF, второй — новый Markdown-отчёт. `--json` дополнительно сохраняет результат для интерфейса. Входной файл и выходы должны иметь разные пути. Прогресс идёт в stderr, итог — в указанные файлы.

## Интерфейс и история

```sh
python -m kiker.web
```

Адрес: [http://127.0.0.1:8765](http://127.0.0.1:8765). Другой порт или отдельная папка истории:

```sh
python -m kiker.web --port 8766 --data-dir ../kiker-reviews
```

История по умолчанию хранится в `.kiker-runs/`, исключённой из Git. Исходный результат модели не меняется при сохранении решений аналитика. Для передачи решений скачайте «Отчёт с решениями аналитика» в паспорте прогона.

На стартовом экране в «Настройках проверки» можно отключить повторное использование готового результата. Оно возможно только при совпадении входных байтов, версии агента, модели и настроек; интерфейс явно сообщает о повторном открытии. Импорт JSON всегда обозначается как сохранённый отчёт. CLI и серии оценки качества выполняют новые запросы.

После обновления кода перезапустите сервер. Если менялись только HTML/CSS, браузеру также нужно обновление страницы.

## Настройки Gemini

`.env` читается из текущей рабочей папки. Переменные процесса переопределяют `.env`, а `--model` переопределяет `KIKER_MODEL`.

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `KIKER_PROVIDER` | `gemini` | `gemini` или `ollama`. |
| `GEMINI_API_KEY` | задаётся локально | Ключ для нового анализа через Gemini. |
| `KIKER_MODEL` | `gemini-3.1-flash-lite` | Имя модели выбранного провайдера. |
| `KIKER_TIMEOUT` | `90` | Таймаут одного запроса, секунды. |
| `KIKER_REQUEST_INTERVAL` | `8` | Минимальный интервал между началами запросов Gemini в одном процессе. |
| `KIKER_THINKING` | `LOW` | Уровень рассуждения; допустимые в клиенте значения — MINIMAL, LOW, MEDIUM, HIGH. Поддержка зависит от модели. |

Настройки опубликованной оценки: `gemini-3.1-flash-lite`, LOW, temperature 0. Температура задаётся клиентом. При смене модели или уровня рассуждения нужна отдельная проверка качества. Ключ создаётся в [Google AI Studio](https://aistudio.google.com/apikey); доступность модели и квоту проверяйте в своём аккаунте.

## Локальный провайдер Ollama

Клиент Ollama включён в код. Запустите локальный сервис, установите выбранную модель и задайте:

```dotenv
KIKER_PROVIDER=ollama
KIKER_MODEL=YOUR_INSTALLED_MODEL
KIKER_TIMEOUT=300
```

Адрес по умолчанию — `http://127.0.0.1:11434`; его меняет `OLLAMA_URL`, размер контекста — `KIKER_CONTEXT`. Публикуемые результаты Gemini не переносятся автоматически на локальную модель.

## Если запуск не удался

| Сообщение или симптом | Действие |
| --- | --- |
| Не задан `GEMINI_API_KEY` | Проверьте локальный `.env` и запуск из корня проекта. Для импорта готового JSON ключ не нужен. |
| Дневной лимит Gemini | Дождитесь восстановления квоты или настройте доступную модель. Неуспешный запуск не считается проверенным ТЗ. |
| Таймаут, временный HTTP 429/503 | Клиент делает ограниченные повторы; после окончательной ошибки можно повторить запуск. |
| PDF без извлечённого текста | Используйте PDF с текстовым слоем или предварительно выполните OCR. |
| Порт занят | Завершите свой предыдущий сервер или используйте `--port 8766`. |
| Замечание исчезло из фильтра после сохранения | Оно перешло в выбранный статус; откройте фильтр «Все». |

## Браузерная проверка

Нужны Node.js и установленный Google Chrome. Можно использовать уже опубликованные результаты, не вызывая Gemini:

```sh
git fetch origin submission
mkdir -p ../kiker-browser-fixtures
git show origin/submission:validation/release-121/regression/kiker-january-conflict-1.json > ../kiker-browser-fixtures/kiker-january-conflict-1.json
git show origin/submission:validation/release-121/regression/kiker-january-conflict-1.md > ../kiker-browser-fixtures/kiker-january-conflict-1.md
git show origin/submission:validation/release-121/regression/kiker-january-corrected-1.json > ../kiker-browser-fixtures/kiker-january-corrected-1.json
python -m kiker.web --port 8766 --data-dir .kiker-test-runs
```

В другом терминале, из корня проекта:

```sh
npm ci
npm run test:browser -- ../kiker-browser-fixtures ../kiker-browser-checks
```

Тест импортирует сохранённые результаты, проверяет цитаты, сохранение решения после перезагрузки, скачанный отчёт, сравнение, мобильную панель и отсутствие ошибок JavaScript. Это автоматическая проверка интерфейса, не человеческий пилот.

## Оценка качества через Gemini

Следующая команда делает новые вызовы модели и расходует квоту API:

```sh
python -m evals.run --output ../kiker-eval --repeats 3 --baseline
```

Используйте новую папку для каждой серии. [Наборы, критерий успеха и контрольные повторы](../evals/README.md).
