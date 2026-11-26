"use strict";

const $ = id => document.getElementById(id);
const state = {token: "", history: [], entry: null, selected: null, file: null, view: "upload", poll: null, selection: 0};
const decisions = {pending: "Без решения", accepted: "Подтверждено", rejected: "Отклонено", clarify: "На уточнении"};
const kinds = {conflict: "Противоречие", ambiguous: "Двусмысленность", gap: "Неполнота", unverifiable: "Непроверяемость", compound: "Смешение требований", prescriptive: "Проектное ограничение"};
const severity = {critical: "Блокирует", major: "Существенно", minor: "Уточнить"};
const sample = `# Учёт расходов по карте

REQ-1. Месяц определяется по дате совершения покупки в Алматы. В расходы входят проведённые покупки. Переводы исключаются.

## Приёмка

| Исходные условия | Действие | Ожидаемый результат |
| --- | --- | --- |
| Покупка 31 января в 23:58 на 4 000 ₸ | Провести 1 февраля в 00:03 по Алматы | Февральский счётчик вырос на 4 000 ₸ |
| Расходы 19 900 ₸ | Перевести 500 ₸ | Счётчик показывает 19 900 ₸ |
`;

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function notice(message) { $("notice").textContent = message; $("notice").hidden = !message; }
async function api(path, payload) {
  const response = await fetch(path, payload === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json", "X-Kiker-Token": state.token}, body: JSON.stringify(payload)
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "Не удалось выполнить действие.");
  return value;
}
function show(view, breadcrumb) {
  state.view = view;
  for (const name of ["upload", "progress", "review", "compare"]) $(name + "-view").hidden = name !== view;
  $("breadcrumb").textContent = breadcrumb;
  if (view !== "progress") clearTimeout(state.poll);
}
function formatDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("ru-RU", {day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit"});
}
function findingCount(value) {
  const ending = value % 100 >= 11 && value % 100 <= 14 ? "замечаний" : value % 10 === 1 ? "замечание" : value % 10 >= 2 && value % 10 <= 4 ? "замечания" : "замечаний";
  return `${value} ${ending}`;
}
function setFile(file) {
  if (!file) return;
  if (file.size > 8 * 1024 * 1024 || !/\.(md|markdown|pdf)$/i.test(file.name)) {
    notice("Выберите Markdown или PDF размером до 8 МБ."); return;
  }
  state.file = file;
  $("file-label").textContent = file.name;
  $("document-text").value = "";
  notice("");
}
function clearFile() { state.file = null; $("document-file").value = ""; $("file-label").textContent = "Выберите файл или перетащите сюда"; }
async function toBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("Не удалось прочитать файл."));
    reader.onload = () => resolve(String(reader.result).split(",")[1]);
    reader.readAsDataURL(file);
  });
}
async function history() {
  state.history = await api("/api/runs");
  const list = $("history"); list.replaceChildren();
  $("history-count").textContent = state.history.length;
  if (!state.history.length) list.append(el("p", "Здесь появятся документы и результаты их проверок.", "empty-history"));
  for (const entry of state.history) {
    const button = el("button", undefined, "history-item" + (state.entry?.id === entry.id ? " active" : ""));
    button.type = "button";
    button.append(el("strong", entry.title), el("small", formatDate(entry.created_at) + " · " + (
      entry.status === "complete" ? findingCount(entry.findings) : entry.status === "error" ? "Ошибка запуска" : "Выполняется"
    )));
    button.title = entry.title;
    button.onclick = () => load(entry.id);
    list.append(button);
  }
}
async function load(id) {
  const selection = ++state.selection;
  try {
    notice("");
    const entry = await api("/api/runs/" + id);
    if (selection !== state.selection) return;
    const previous = state.entry?.id;
    state.entry = entry;
    if (previous !== id) state.selected = null;
    localStorage.setItem("kiker.lastRun", id);
    if (entry.status === "complete") {
      show("review", "Результат проверки"); renderReview();
    } else {
      show("progress", "Проверка документа");
      $("progress-title").textContent = entry.title;
      $("progress-step").textContent = entry.status === "error" ? "Проверка не завершилась" : entry.step;
      $("progress-description").textContent = entry.error || "Результат сохранится в истории. Можно продолжить работу в другой вкладке.";
      $("progress-view").querySelector(".spinner").hidden = entry.status === "error";
      $("elapsed").textContent = entry.status === "error" ? "Отчёт не создан" : `Прошло ${Math.max(0, Math.floor((Date.now() - new Date(entry.created_at)) / 1000))} с`;
      if (entry.status !== "error") {
        clearTimeout(state.poll);
        state.poll = setTimeout(() => { if (state.view === "progress" && state.entry?.id === id) load(id); }, 1500);
      }
    }
    await history();
  } catch (error) { notice(error.message); }
}
function decisionFor(id) { return state.entry.decisions.items[id] || {decision: "pending", note: ""}; }
function renderReview() {
  $("save-status").textContent = "";
  const entry = state.entry, result = entry.result;
  $("run-origin").textContent = entry.imported ? "СОХРАНЁННЫЙ ОТЧЁТ · БЕЗ НОВОГО ЗАПРОСА" : "РЕЗУЛЬТАТ ПРОВЕРКИ";
  $("review-title").textContent = result.document.title;
  $("review-meta").textContent = `${result.document.name} · ${formatDate(result.run.date)} · ${result.run.model} · ${result.run.seconds.toFixed(1)} с`;
  $("metric-total").textContent = result.findings.length;
  $("metric-critical").textContent = result.findings.filter(f => f.severity === "critical").length;
  $("metric-requirements").textContent = result.requirements.length;
  $("metric-reviewed").textContent = `${result.findings.filter(f => decisionFor(f.id).decision !== "pending").length} / ${result.findings.length}`;
  $("review-limitations").textContent = (result.rejected ? `${result.rejected} записей модели не прошли проверку. ` : "") +
    (result.document.empty_pages?.length ? "В PDF есть страницы без извлечённого текста. " : "") +
    "Проверьте смысл замечаний по цитатам. Решения аналитика сохраняются отдельно от исходного отчёта.";
  const base = "/api/runs/" + entry.id;
  $("download-report").href = base + "/report.md";
  $("download-json").href = base + "/result.json";
  $("download-decisions").href = base + "/review.json";
  const fields = $("passport-fields"); fields.replaceChildren();
  for (const [label, value] of [["Модель", result.run.model], ["Версия агента", result.run.version], ["SHA-256 входа", result.document.sha256], ["Запросы", result.run.requests], ["Входные токены", result.run.input_tokens], ["Выходные токены", result.run.output_tokens]]) fields.append(el("dt", label), el("dd", String(value)));
  renderFindings();
}
function renderFindings() {
  const all = state.entry.result.findings, filter = $("finding-filter").value;
  const findings = all.filter(f => filter === "all" || decisionFor(f.id).decision === filter);
  if (!findings.some(f => f.id === state.selected)) state.selected = findings[0]?.id || null;
  const list = $("findings-list"); list.replaceChildren();
  if (!findings.length) list.append(el("p", all.length ? "В этом фильтре замечаний нет." : "Замечаний не найдено.", "empty-result"));
  for (const finding of findings) {
    const button = el("button", undefined, "finding-card" + (state.selected === finding.id ? " active" : ""));
    button.type = "button";
    button.setAttribute("aria-pressed", state.selected === finding.id ? "true" : "false");
    button.append(el("span", `${finding.id} · ${severity[finding.severity]}`, "tag " + finding.severity), el("h3", finding.description), el("small", `${kinds[finding.kind]} · ${decisions[decisionFor(finding.id).decision]}`));
    button.onclick = () => { state.selected = finding.id; renderFindings(); };
    list.append(button);
  }
  renderDetail(all.find(f => f.id === state.selected));
}
function renderDetail(finding) {
  const panel = $("finding-detail"); panel.replaceChildren();
  if (!finding) {
    const empty = el("div", undefined, "empty-result");
    empty.append(el("strong", state.entry.result.findings.length ? "Выберите другое представление" : "Можно перейти к ручному согласованию"),
      el("p", "Отсутствие замечаний модели не гарантирует полноту и корректность ТЗ. Проверьте ключевые сценарии с автором."));
    panel.append(empty); return;
  }
  const top = el("div", undefined, "detail-top");
  top.append(el("span", `${finding.id} · ${kinds[finding.kind]}`, "tag"), el("span", severity[finding.severity], "tag " + finding.severity));
  panel.append(top, el("h2", finding.description), el("div", "Основание в документе", "subheading"));
  for (const sourceId of finding.sources) {
    const source = state.entry.result.document.blocks.find(b => b.id === sourceId);
    const quote = el("blockquote", undefined, "quote");
    const location = `${source.page ? `стр. ${source.page} · ` : ""}строки ${source.start}${source.end !== source.start ? "–" + source.end : ""}`;
    quote.append(el("cite", `${source.original_id || source.id} · ${location}`), el("p", source.text));
    panel.append(quote);
  }
  panel.append(el("div", "Вопрос автору ТЗ", "subheading"), el("p", finding.question, "question"));
  const form = el("form", undefined, "decision-form"), selected = decisionFor(finding.id);
  const options = el("fieldset", undefined, "decision-options");
  const legend = el("legend", "Ваше решение", "subheading"); options.append(legend);
  for (const [value, label] of Object.entries(decisions)) {
    const item = el("label"), input = document.createElement("input");
    input.type = "radio"; input.name = "decision"; input.value = value; input.checked = selected.decision === value;
    item.append(input, document.createTextNode(label)); options.append(item);
  }
  const label = el("label", "Комментарий или согласованное исправление", "field-label");
  label.htmlFor = "decision-note";
  const note = el("textarea"); note.id = "decision-note"; note.rows = 3; note.maxLength = 2000; note.value = selected.note;
  note.placeholder = "Например: месяц учёта — январь, исправить критерий приёмки.";
  const bottom = el("div", undefined, "decision-bottom"), submit = el("button", "Сохранить решение", "primary"), message = el("span");
  submit.type = "submit"; message.setAttribute("role", "status"); bottom.append(submit, message);
  form.append(options, label, note, bottom);
  form.onsubmit = async event => {
    event.preventDefault(); submit.disabled = true; notice("");
    const entryId = state.entry.id;
    try {
      const record = await api(`/api/runs/${entryId}/decisions`, {id: finding.id, decision: new FormData(form).get("decision"), note: note.value, revision: state.entry.decisions.revision});
      if (state.entry?.id !== entryId) return;
      state.entry.decisions = record;
      renderReview();
      $("save-status").textContent = "Решение сохранено";
    } catch (error) { notice(error.message); submit.disabled = false; }
  };
  panel.append(form);
}
async function openCompare() {
  await history();
  const completed = state.history.filter(item => item.status === "complete");
  if (completed.length < 2) { notice("Для сравнения нужны две завершённые проверки. Загрузите исправленное ТЗ или откройте второй сохранённый отчёт."); return; }
  for (const name of ["compare-before", "compare-after"]) {
    $(name).replaceChildren();
    for (const entry of completed) {
      const option = el("option", `${entry.name} · ${formatDate(entry.created_at)} · ${findingCount(entry.findings)}`); option.value = entry.id; $(name).append(option);
    }
  }
  $("compare-after").value = state.entry.id;
  $("compare-before").value = completed.find(item => item.id !== state.entry.id).id;
  $("comparison-result").replaceChildren();
  show("compare", "Сравнение версий"); notice("");
}
function renderComparison(data) {
  const root = $("comparison-result"); root.replaceChildren();
  if (data.same_input) root.append(el("p", "Исходный файл одинаковый: сравниваются повторные проверки одного документа.", "info-note"));
  const columns = el("div", undefined, "compare-columns");
  for (const [title, ids, mode] of [["Не повторилось", data.not_repeated, "before"], ["Сопоставлено", data.matched, "matched"], ["Новое", data.new, "after"]]) {
    const column = el("section", undefined, "paper compare-column"); column.append(el("h2", `${title} · ${ids.length}`));
    if (!ids.length) column.append(el("p", "Нет замечаний", "muted"));
    for (const id of ids) {
      const entry = mode === "before" ? data.before : data.after;
      const finding = entry.result.findings.find(f => f.id === (mode === "matched" ? id.after : id));
      const card = el("div", undefined, "comparison-item");
      card.append(el("span", `${finding.id} · ${severity[finding.severity]}`, "tag " + finding.severity), el("p", finding.description));
      if (mode === "matched") {
        const old = data.before.result.findings.find(f => f.id === id.before);
        const details = el("details"); details.append(el("summary", "Предыдущая формулировка"), el("p", old.description)); card.append(details);
      }
      column.append(card);
    }
    columns.append(column);
  }
  root.append(columns);
}

$("new-review").onclick = $("back-from-progress").onclick = () => { ++state.selection; show("upload", "Новая проверка"); notice(""); };
$("document-file").onchange = event => setFile(event.target.files[0]);
$("document-text").oninput = clearFile;
$("clear-input").onclick = () => { clearFile(); $("document-text").value = ""; };
$("load-sample").onclick = () => { clearFile(); $("document-text").value = sample; $("document-text").focus(); };
$("drop-zone").onkeydown = event => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); $("document-file").click(); } };
for (const name of ["dragenter", "dragover"]) $("drop-zone").addEventListener(name, event => { event.preventDefault(); $("drop-zone").classList.add("dragging"); });
for (const name of ["dragleave", "drop"]) $("drop-zone").addEventListener(name, event => { event.preventDefault(); $("drop-zone").classList.remove("dragging"); if (name === "drop") setFile(event.dataTransfer.files[0]); });
$("upload-form").onsubmit = async event => {
  event.preventDefault(); notice("");
  const text = $("document-text").value.trim();
  if (!state.file && !text) { notice("Добавьте файл или вставьте текст требований."); $("document-text").focus(); return; }
  $("submit-review").disabled = true;
  try {
    const file = state.file || new File([text], "requirements.md", {type: "text/markdown"});
    const entry = await api("/api/runs", {name: file.name, data: await toBase64(file), reuse: $("reuse-result").checked});
    await load(entry.id);
    if (entry.reused) $("save-status").textContent = "Открыт готовый отчёт: документ, версия агента и настройки совпадают. Новый запрос к модели не выполнялся.";
  } catch (error) { notice(error.message); }
  finally { $("submit-review").disabled = false; }
};
$("import-button").onclick = () => $("import-file").click();
$("import-file").onchange = async event => {
  const file = event.target.files[0]; if (!file) return;
  try {
    if (file.size > 8 * 1024 * 1024) throw new Error("Отчёт превышает 8 МБ.");
    const entry = await api("/api/import", JSON.parse(await file.text()));
    await load(entry.id);
  } catch (error) { notice(error instanceof SyntaxError ? "Не удалось прочитать JSON-отчёт." : error.message); }
  finally { event.target.value = ""; }
};
$("finding-filter").onchange = renderFindings;
$("open-compare").onclick = () => openCompare().catch(error => notice(error.message));
$("back-to-review").onclick = () => { show("review", "Результат проверки"); renderReview(); };
$("compare-form").onsubmit = async event => {
  event.preventDefault(); notice("");
  const before = $("compare-before").value, after = $("compare-after").value;
  if (before === after) { notice("Выберите две разные проверки."); return; }
  try { renderComparison(await api(`/api/comparison?before=${before}&after=${after}`)); }
  catch (error) { notice(error.message); }
};
(async () => {
  try {
    state.token = (await api("/api/session")).token;
    await history();
    const last = localStorage.getItem("kiker.lastRun");
    if (last && state.history.some(entry => entry.id === last)) await load(last);
  } catch (error) { notice("Не удалось подключиться к локальному серверу. " + error.message); }
})();
