"use strict";

// Пользовательские тексты создаются только через textContent/Text, без HTML-интерпретации.
const $ = (selector) => document.querySelector(selector);
function node(tag, attrs = {}, ...children) {
  const result = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith("on") && typeof value === "function") result.addEventListener(key.slice(2), value);
    else if (["disabled", "checked", "hidden", "required", "readOnly"].includes(key)) result[key] = Boolean(value);
    else if (value !== undefined && value !== null) result.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child !== null && child !== undefined) result.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return result;
}
const button = (text, action, attrs = {}) => node("button", { type: "button", class: "button", onclick: action, ...attrs }, text);
const pill = (text, style = "") => node("span", { class: `chip ${style}` }, text);
const date = (value) => value ? new Date(value).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }) : "";
const prose = (text) => node("div", { class: "prose" }, text || "");
const state = { drafts: new Map(), pending: new Set(), sequence: 0, documentReturn: "#documents" };
const reasons = {
  no_evidence: "В доступных документах не найдено подтверждений.", no_answer: "Документы не содержат запрошенных сведений.",
  partial_coverage: "Подтверждена только часть ответа.", conflicting_sources: "Обнаружены противоречащие сведения.",
  low_confidence: "Модель оценила подтверждения как недостаточные.", no_verified_sources: "Нет проверенных источников.",
  invalid_model_output: "Ответ не прошёл проверку формата или источников.", sources_changed: "Источники изменились во время обработки.",
  price_requires_confirmation: "Точную стоимость нужно уточнить у менеджера.", ambiguous_context: "Нужно уточнить смысл вопроса.",
  operational_confirmation_required: "Выезд или назначение сотрудника подтверждает менеджер по актуальным данным.",
  invalid_rewrite: "Не удалось однозначно сформулировать вопрос с учётом беседы.",
  provider_timeout: "Сервис ответа не завершил обработку вовремя.", provider_not_configured: "Сервис ответов пока не настроен.",
  provider_budget_exceeded: "Достигнут лимит обращений к сервису ответов.", provider_balance_exhausted: "Сервис ответов временно недоступен.",
  operation_interrupted: "Операция прервалась до завершения.", vector_unavailable: "Поиск по документам временно недоступен.",
};
const reason = (code) => reasons[code] || "Результат требует уточнения. Подробности доступны в журнале обработки.";

async function api(path, { method = "GET", body, raw = false } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  try {
    // Свежий CSRF нужен также после входа в другой вкладке. Платный запрос отправляется один раз.
    if (method !== "GET" && path !== "/auth/login") {
      const sessionResponse = await fetch("/auth/session", { credentials: "same-origin", redirect: "error" });
      if (!sessionResponse.ok) throw Object.assign(new Error("Сессия закончилась. Войдите снова; набранный вопрос сохранён в этой вкладке."), { status: 401 });
      headers["X-CSRF-Token"] = (await sessionResponse.json()).csrf_token;
    }
    const response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin", redirect: "error" });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw Object.assign(new Error(data.error?.message || "Не удалось выполнить запрос. Повторите позже."), { status: response.status, code: data.error?.code, requestId: data.request_id });
    }
    return raw ? response : await response.json();
  } catch (error) {
    if (error.status) throw error;
    throw new Error("Не удалось связаться с сервером. Проверьте историю перед повторной отправкой: операция могла сохраниться.");
  }
}

function notify(message, error = false, expired = false) {
  const box = $("#feedback");
  box.hidden = !message;
  box.className = `notice ${error ? "error" : ""}`;
  box.setAttribute("role", error ? "alert" : "status");
  box.replaceChildren(document.createTextNode(message || ""));
  if (expired) box.append(" ", node("a", { href: "/login", target: "_blank", rel: "noopener" }, "Войти в новой вкладке"));
}
function showError(error) { notify(error.message, true, error.status === 401); }
function route() {
  const [path = "dialogs", query = ""] = location.hash.slice(1).split("?");
  const [section = "dialogs", id] = path.split("/");
  return { section: section || "dialogs", id, query: new URLSearchParams(query) };
}
function pagination(section, count, offset, query = {}) {
  const link = (value) => `#${section}?${new URLSearchParams({ ...query, offset: String(value) })}`;
  return node("nav", { class: "pagination", "aria-label": "Страницы списка" },
    offset ? node("a", { href: link(Math.max(0, offset - 100)), class: "button" }, "Назад") : node("span"),
    node("span", { class: "small muted" }, offset || count ? `${offset + 1}–${offset + count}` : "Нет записей"),
    count === 100 ? node("a", { href: link(offset + 100), class: "button" }, "Далее") : node("span"));
}
function heading(title, subtitle, action) {
  return node("header", { class: "page-heading" }, node("div", {}, node("span", { class: "eyebrow" }, "Рабочее пространство"), node("h1", {}, title), node("p", { class: "muted" }, subtitle)), action);
}
function empty(title, text) {
  return node("div", { class: "empty-state" }, node("div", { class: "empty-symbol", "aria-hidden": "true" }, "✧"), node("h2", {}, title), node("p", { class: "muted" }, text));
}
function busy(id) { return state.pending.has(id); }

function modal(title, fields, submitText, action, { danger = false, hint = "" } = {}) {
  const dialog = $("#action-dialog");
  const previous = document.activeElement;
  const error = node("p", { class: "form-error", role: "alert", hidden: true });
  const submit = node("button", { type: "submit", class: `button ${danger ? "danger" : "primary"}` }, submitText);
  const cancel = button("Отмена", () => dialog.close());
  let working = false;
  const form = node("form", { onsubmit: async (event) => {
    event.preventDefault();
    if (working) return;
    const fieldStates = [...form.querySelectorAll("input, textarea")].map((input) => [input, input.readOnly]);
    fieldStates.forEach(([input]) => { input.readOnly = true; });
    working = true; submit.disabled = true; cancel.disabled = true; error.hidden = true;
    submit.textContent = "Выполняется…";
    try { await action(form); dialog.close(); }
    catch (failure) { error.textContent = failure.message; error.hidden = false; }
    finally { working = false; submit.disabled = false; cancel.disabled = false; submit.textContent = submitText; fieldStates.forEach(([input, previous]) => { input.readOnly = previous; }); }
  } }, node("h2", { id: "dialog-title" }, title), hint ? node("p", { class: "muted" }, hint) : null, fields, error,
  node("div", { class: "actions" }, cancel, submit));
  $("#dialog-content").replaceChildren(form);
  dialog.oncancel = (event) => { if (working) event.preventDefault(); };
  dialog.onclose = () => { if (previous?.isConnected) previous.focus(); else $("#main").focus({ preventScroll: true }); };
  dialog.showModal();
}
function field(label, name, { multiline = false, value = "", maxLength = 200, ...attrs } = {}) {
  const input = node(multiline ? "textarea" : "input", { id: `field-${name}`, name, maxlength: maxLength, required: true, ...attrs });
  input.value = value;
  return [node("label", { for: `field-${name}` }, label), input];
}
async function createConversation(event) {
  if (busy("create")) return;
  state.pending.add("create");
  const control = event?.currentTarget;
  if (control) control.disabled = true;
  try { const result = await api("/conversations", { method: "POST", body: {} }); location.hash = `dialogs/${result.id}`; }
  catch (error) { showError(error); }
  finally { state.pending.delete("create"); if (control?.isConnected) control.disabled = false; }
}
async function conversationAction(cid, action, message, { revealResult = false } = {}) {
  if (busy(cid)) return;
  state.pending.add(cid); render();
  try {
    const result = await action();
    if (revealResult && route().section === "dialogs" && route().id === cid) {
      const query = route().query;
      query.set("run", result.operation_id);
      window.history.replaceState(null, "", `#dialogs/${cid}?${query.toString()}`);
    }
    notify(message);
  }
  catch (error) { showError(error); }
  finally { state.pending.delete(cid); if (route().id === cid || route().section === "history" || !route().id) render(); }
}
function renameConversation(conversation) {
  modal("Переименовать диалог", field("Название", "title", { value: conversation.title }), "Сохранить", async (form) => {
    await api(`/conversations/${conversation.id}`, { method: "PATCH", body: { title: new FormData(form).get("title") } });
    notify("Название сохранено."); render();
  });
}
function deleteConversation(conversation) {
  modal("Удалить диалог?", node("p", {}, `«${conversation.title}»: вопросы, ответы и итоги будут удалены. Документы компании сохранятся.`), "Удалить диалог", async () => {
    await api(`/conversations/${conversation.id}?confirm=true`, { method: "DELETE" });
    state.drafts.delete(conversation.id); location.hash = "dialogs"; notify("Диалог удалён.");
  }, { danger: true });
}
async function exportConversation(cid) {
  try {
    const response = await api(`/conversations/${cid}/export`, { raw: true });
    const url = URL.createObjectURL(await response.blob());
    const anchor = node("a", { href: url, download: "conversation.json" });
    document.body.append(anchor); anchor.click(); anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (error) { showError(error); }
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); notify("Текст скопирован."); }
  catch (_) {
    const area = node("textarea", { readOnly: true, "aria-label": "Текст для копирования" }); area.value = text;
    modal("Скопируйте текст", area, "Готово", async () => {}, { hint: "Автоматическое копирование недоступно. Выделите текст и скопируйте его." });
    area.focus(); area.select();
  }
}
function sourcesView(sources = []) {
  if (!sources.length) return null;
  const unique = [...new Map(sources.map((s) => [JSON.stringify(s), s])).values()];
  return node("details", { class: "sources" }, node("summary", {}, `Источники (${unique.length})`), unique.map((source) => node("article", { class: "source" },
    node("h3", {}, source.current_title || source.title), node("blockquote", {}, source.quote), source.document_id ? node("a", {
      href: `#documents/${encodeURIComponent(source.document_id)}`, class: "button", onclick: () => { state.documentReturn = location.hash; },
    }, "Открыть документ") : node("p", { class: "small muted" }, "Контекст запроса, не документ компании"))));
}
function runCard(run) {
  const isSummary = run.kind === "summary";
  const result = run.result || {};
  const working = run.state === "running";
  const failed = ["failed", "interrupted"].includes(run.state);
  const disabled = working || busy(run.conversation_id);
  const label = isSummary ? "Итог беседы" : "Помощник";
  const sources = isSummary ? (result.findings || []).flatMap((finding) => finding.sources) : run.sources;
  const body = node("article", { class: `card assistant-message ${isSummary ? "summary-card" : ""}`, "aria-label": label },
    node("div", { class: "actions spread" }, node("span", { class: "message-label" }, label),
      working ? pill("Выполняется…", "neutral") : failed ? pill("Не завершено", "error") : isSummary ? pill(run.is_stale ? "Устарел" : "Снимок беседы", run.is_stale ? "warning" : "") :
        pill(run.needs_review ? (run.reviewed_at ? "Требовало проверки" : "Нужна проверка") : "Есть источники", run.needs_review ? "warning" : "")),
    node("p", { class: "record-meta" }, date(run.created_at)),
    failed ? node("div", { class: "notice error" }, reason(run.error)) : null,
    isSummary && run.is_stale ? node("p", { class: "notice warning" }, "После этого итога беседа или отметки изменились. Создайте новый итог для актуального снимка.") : null,
    prose(run.display_text || run.answer || (working ? "Результат появится после завершения обработки. Обновите диалог через некоторое время." : "Ответ не был получен.")),
    !isSummary && run.needs_review ? node("ul", { class: "reason-list" }, (result.review_reasons || [run.error]).filter(Boolean).map((code) => node("li", {}, reason(code)))) : null,
    run.reviewed_at ? node("p", { class: "small" }, pill("Проверка отмечена"), ` ${date(run.reviewed_at)}. Результат уточнения не записан.`) : null,
    sourcesView(sources),
    node("div", { class: "actions" },
      run.answer && !failed && !working ? button("Копировать", () => copyText(isSummary ? (run.display_text || result.text || run.answer) : run.answer)) : null,
      !isSummary && run.needs_review && !run.reviewed_at && !working ? button("Отметить проверенным", () => conversationAction(run.conversation_id || run.id,
        () => api(`/qa/${run.id}/review`, { method: "POST" }), "Факт проверки отмечен. Исходная причина сохранена."), { disabled }) : null),
    node("details", {}, node("summary", {}, "Сведения об обработке"),
      node("p", { class: "small muted" }, `Операция: ${run.id}`),
      run.search_question ? node("p", { class: "small" }, `Поиск: ${run.search_question}`) : null,
      run.model ? node("p", { class: "small muted" }, `Модель: ${run.model}`) : null));
  return node("section", { class: "turn", id: `run-${run.id}` },
    !isSummary ? node("div", { class: "user-message" }, node("p", { class: "message-label" }, "Менеджер"), prose(run.question)) : null, body);
}
function compose(conversation) {
  const cid = conversation.id;
  const textarea = node("textarea", { id: "question", name: "question", rows: "3", maxlength: "2000", disabled: conversation.busy || busy(cid), placeholder: "Например: входит ли чистка духовки в генеральную уборку?", required: true,
    oninput: (event) => state.drafts.set(cid, event.target.value),
    onkeydown: (event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); event.target.form.requestSubmit(); } },
  });
  textarea.value = state.drafts.get(cid) || "";
  const unavailable = conversation.busy || busy(cid) || !conversation.budget.can_ask;
  const submit = node("button", { type: "submit", class: "button primary", disabled: unavailable }, busy(cid) ? "Готовим ответ…" : "Отправить вопрос");
  const form = node("form", { class: "card composer", onsubmit: async (event) => {
    event.preventDefault();
    if (unavailable || busy(cid)) return;
    const question = textarea.value;
    state.drafts.set(cid, question); state.pending.add(cid); textarea.disabled = true; submit.disabled = true; submit.textContent = "Готовим ответ…";
    try {
      const answer = await api("/kb/ask", { method: "POST", body: { question, conversation_id: cid } });
      state.drafts.delete(cid);
      if (route().id === cid) { location.hash = `dialogs/${cid}?run=${answer.operation_id}`; notify(""); }
      else notify("Ответ сохранён в диалоге.");
    } catch (error) { showError(error); }
    finally { state.pending.delete(cid); if (route().id === cid || !route().id) render(); }
  } }, node("label", { for: "question" }, "Вопрос по обращению"), textarea,
  !conversation.budget.can_ask ? node("p", { class: "notice warning" }, "Достигнут предел беседы. Вопрос сохранён в поле. Начните новый диалог; итог этой беседы по-прежнему доступен.") : null,
  conversation.busy ? node("p", { class: "notice" }, "В этом диалоге выполняется операция. Нажмите «Обновить», чтобы получить её результат.") : null,
  node("div", { class: "actions spread" }, node("span", { class: "small muted" }, "Enter — новая строка · Ctrl/⌘ + Enter — отправить"), submit));
  return form;
}
async function dialogsView(id, query) {
  const offset = Math.max(0, Number(query.get("offset")) || 0);
  const [conversations, selected] = await Promise.all([api(`/conversations?limit=100&offset=${offset}`), id ? api(`/conversations/${encodeURIComponent(id)}`) : null]);
  const list = node("aside", { class: "card conversation-list-panel", hidden: Boolean(id), "aria-label": "Сохранённые диалоги" },
    node("h2", {}, "Обращения"), button("＋ Новый диалог", createConversation, { class: "button primary full" }),
    node("div", { class: "conversation-list" }, conversations.length ? conversations.map((item) => node("a", { class: "conversation-link", href: `#dialogs/${item.id}`, "aria-current": String(item.id === id) },
      node("strong", {}, item.title), node("span", { class: "small" }, date(item.updated_at)), item.pending_reviews ? pill(`К проверке: ${item.pending_reviews}`, "warning") : null,
      item.busy ? pill("Выполняется…", "neutral") : null)) : node("p", { class: "muted small" }, "Здесь появятся ваши сохранённые обращения.")), pagination("dialogs", conversations.length, offset));
  const thread = node("section", { class: "thread-pane", "aria-label": "Беседа" });
  if (selected) {
    const blocked = selected.busy || busy(id);
    const budget = selected.budget;
    thread.append(node("header", { class: "thread-header", id: "thread-start" }, node("a", { class: "button quiet thread-back", href: "#dialogs" }, "← Все диалоги"), node("h2", {}, selected.title),
      node("div", { class: "actions" }, button("Переименовать", () => renameConversation(selected), { disabled: blocked }), button("Экспорт JSON", () => exportConversation(id)), button("Удалить", () => deleteConversation(selected), { class: "button danger", disabled: blocked }), button("Обновить", () => render()),
        button("↓ К концу диалога", () => $("#thread-end")?.scrollIntoView({ block: "start" }))),
      node("p", { class: "thread-budget" }, `Контекст: ${budget.used_tokens.toLocaleString("ru-RU")} / ${budget.max_tokens.toLocaleString("ru-RU")} токенов · следующий ответ требует резерва`),
      budget.warning && budget.can_ask ? node("p", { class: "notice warning" }, "Беседа приближается к пределу. Для отдельного обращения начните новый диалог.") : null));
    if (selected.runs.length) thread.append(node("div", { class: "timeline" }, selected.runs.map(runCard)));
    else {
      const intro = empty("С чего начнём?", "Задайте вопрос об услуге или условиях. Помощник найдёт ответ в документах компании.");
      intro.classList.add("card");
      intro.append(node("div", { class: "suggestions" }, ["Что входит в генеральную уборку?", "Как подготовить квартиру к уборке?", "Что уточнить перед уборкой после ремонта?"].map((text) => button(text, () => { state.drafts.set(id, text); const input = $("#question"); input.value = text; input.focus(); }))));
      thread.append(intro);
    }
    thread.append(node("div", { class: "actions spread thread-footer", id: "thread-end" },
      button(busy(id) ? "Выполняется…" : "Подвести итог", () => conversationAction(id, () => api(`/conversations/${id}/summary`, { method: "POST" }), "Итог сохранён в беседе.", { revealResult: true }), { disabled: blocked || !selected.runs.some((r) => r.kind === "answer") }),
      button("↑ К началу диалога", () => $("#thread-start")?.scrollIntoView({ block: "start" }))), compose(selected));
  } else {
    thread.classList.add("card");
    thread.append(empty("Каждому обращению — свой диалог", "Продолжите сохранённую беседу или начните новую. История поможет задавать уточняющие вопросы."), button("Начать диалог", createConversation, { class: "button primary" }));
  }
  return node("div", {}, heading("Диалоги", "Ответы по документам, контекст обращения и понятные уточнения."), node("div", { class: "dialogs-layout" }, list, thread));
}
const indexingLabel = (doc) => !doc.is_active ? "Исключён из поиска" : ({ ready: "Готов к поиску", failed: "Ошибка индексации", pending: "Ожидает индексации", indexing: "Индексируется…" }[doc.indexing_status] || "Не готов");
function addDocument() {
  modal("Добавить документ", [...field("Название документа", "title"), ...field("Текст документа", "text", { multiline: true, maxLength: 100000 })], "Добавить и обработать", async (form) => {
    const values = new FormData(form);
    const doc = await api("/kb/documents", { method: "POST", body: { title: values.get("title"), text: values.get("text") } });
    state.documentReturn = "#documents"; location.hash = `documents/${doc.id}`;
    notify(doc.indexing_status === "ready" ? "Документ добавлен и доступен для поиска." : "Документ сохранён, но обработка не завершилась. Откройте его и повторите индексацию.", doc.indexing_status !== "ready");
  }, { hint: "Добавьте условия или регламент обычным текстом. Для изменения правил создайте новый документ и исключите прежний." });
}
async function documentsView(id, query) {
  if (id) {
    const doc = await api(`/kb/documents/${encodeURIComponent(id)}`);
    const blocked = busy(id);
    return node("div", {}, node("a", { class: "button quiet", href: state.documentReturn }, "← Назад"),
      heading(doc.title, "Документ базы знаний"), node("article", { class: "card" }, node("div", { class: "document-meta" }, pill(indexingLabel(doc), doc.indexing_status === "failed" ? "error" : !doc.is_active ? "neutral" : ""), node("span", { class: "record-meta" }, `Добавлен ${date(doc.created_at)}`)),
      doc.error ? node("p", { class: "notice error" }, reason(doc.error)) : null,
      node("div", { class: "actions" }, button("Переименовать", () => modal("Переименовать документ", field("Название документа", "title", { value: doc.title, maxlength: "200" }), "Сохранить", async (form) => {
        await api(`/kb/documents/${id}`, { method: "PATCH", body: { title: new FormData(form).get("title") } });
        notify("Название документа сохранено. Повторная индексация не требуется."); render();
      }), { disabled: blocked }), doc.is_active ? button(blocked ? "Выполняется…" : "Повторить индексацию", () => conversationAction(id, () => api(`/kb/documents/${id}/reindex`, { method: "POST" }), "Состояние обработки обновлено."), { disabled: blocked }) : null,
        doc.is_active ? button("Исключить из поиска", () => modal("Исключить документ?", node("p", {}, "Он перестанет участвовать в новых ответах. Цитаты в прошлых ответах сохранятся."), "Исключить", async () => { await api(`/kb/documents/${id}/deactivate`, { method: "POST" }); notify("Документ исключён из поиска."); render(); }), { disabled: blocked }) : null),
      node("div", { class: "prose document-text" }, doc.text)));
  }
  const offset = Math.max(0, Number(query.get("offset")) || 0);
  const docs = await api(`/kb/documents?limit=100&offset=${offset}`);
  return node("div", {}, heading("Документы", "Услуги, ограничения и условия — единая база для ответов.", button("＋ Добавить документ", addDocument, { class: "button primary" })),
    node("div", { class: "record-list" }, docs.length ? docs.map((doc) => node("article", { class: "card" }, pill(indexingLabel(doc), doc.indexing_status === "failed" ? "error" : !doc.is_active ? "neutral" : ""), node("h2", {}, doc.title), node("p", { class: "record-meta" }, date(doc.created_at)), node("a", { class: "button", href: `#documents/${doc.id}`, onclick: () => { state.documentReturn = `#documents?offset=${offset}`; } }, "Открыть"))) : node("div", { class: "card" }, empty("База знаний пока пуста", "Добавьте первый документ с описанием услуги или условий работы."))), pagination("documents", docs.length, offset));
}
const actions = { auth_login: "Вход в панель", auth_logout: "Выход из панели", document_added: "Документ добавлен", document_indexed: "Индексация документа", document_deactivated: "Документ исключён", kb_answer: "Обработка вопроса", answer_with_context: "Ответ по переданному контексту", conversation_created: "Диалог создан", conversation_renamed: "Диалог переименован", conversation_deleted: "Диалог удалён", conversation_operation_started: "Обработка начата", conversation_summarized: "Подведение итога", answer_reviewed: "Проверка отмечена" };
Object.assign(actions, { document_renamed: "Документ переименован", provider_limits_updated: "Лимиты провайдера изменены", provider_budget_reset: "Суточные счётчики сброшены" });

async function settingsView() {
  const data = await api("/settings/provider");
  const labels = { calls_per_minute: "Вызовы за 60 секунд", calls_per_day: "Вызовы за сутки", token_budget_per_day: "Суточный резерв токенов" };
  const ranges = { calls_per_minute: [1, 100], calls_per_day: [1, 10000], token_budget_per_day: [1000, 100000000] };
  const number = (value) => Number(value).toLocaleString("ru-RU");
  const blocked = busy("settings");
  const metrics = node("div", { class: "settings-grid" }, Object.keys(labels).map((key) => node("article", { class: "card", "aria-label": labels[key] },
    node("h2", {}, labels[key]), node("p", {}, `${number(data.usage[key])} / ${number(data.limits[key])}`),
    node("p", { class: "small muted" }, `Осталось: ${number(data.remaining[key])}`))));
  const form = node("form", { class: "card", onsubmit: async (event) => {
    event.preventDefault();
    if (busy("settings")) return;
    const values = Object.fromEntries(Object.keys(labels).map((key) => [key, Number(new FormData(form).get(key))]));
    state.pending.add("settings");
    let saved = false;
    form.querySelectorAll("input, button").forEach((control) => { control.disabled = true; });
    try { await api("/settings/provider", { method: "PUT", body: values }); saved = true; notify("Лимиты сохранены и применены без перезапуска."); }
    catch (error) { showError(error); }
    finally {
      state.pending.delete("settings");
      if (route().section === "settings" && saved) render();
      else form.querySelectorAll("input, button").forEach((control) => { control.disabled = false; });
    }
  } }, node("h2", {}, "Настройки лимитов"),
  node("div", { class: "settings-grid" }, Object.keys(labels).map((key) => node("div", { class: "settings-field" }, field(labels[key], key, {
    type: "number", value: data.limits[key], min: ranges[key][0], max: ranges[key][1], step: "1", disabled: blocked,
  })))), node("p", { class: "small muted" }, "Сохранённые значения действуют со следующего вызова и сохраняются после перезапуска. Если снизить лимит ниже расхода, новые вызовы будут заблокированы."),
  button("Сохранить", () => form.requestSubmit(), { class: "button primary", disabled: blocked }));
  const actual = data.actual_usage_today;
  return node("div", {}, heading("Расход и лимиты", "Общий учёт вызовов модели и эмбеддингов.", button("Обновить", render)), metrics,
    node("p", { class: "notice" }, "Резерв — консервативная оценка для ограничения запросов, а не стоимость и не фактически оплаченные токены. Следующий вызов должен целиком помещаться в оставшийся резерв."),
    node("p", { class: "small muted" }, `По данным провайдера за сегодня UTC: ${number(actual.total_tokens)} токенов. Статистика токенов получена для ${actual.reported_calls} из ${actual.all_calls} вызовов. Эти данные не обнуляются при сбросе счётчиков.`),
    form, node("section", { class: "card settings-reset" }, node("h2", {}, "Суточный учёт"),
      node("p", {}, `Начало текущего отсчёта: ${date(data.period_started_at)}. Следующий суточный период: ${date(data.next_day_at)} (00:00 UTC).`),
      node("p", { class: "small", "data-last-reset": "" }, data.last_reset ? `Последний сброс: ${date(data.last_reset.at)}. Выполнил: ${data.last_reset.actor_type === "api_key" ? "Сервисный API" : data.last_reset.actor_id}.` : "Сбросов ещё не было."),
      node("p", { class: "small muted" }, "Сброс начинает новый отсчёт суточных вызовов и резерва до ближайших 00:00 UTC. История, фактический usage и минутное ограничение сохраняются. При текущих операциях сброс недоступен."),
      button("Сбросить суточные счётчики", () => modal("Сбросить суточные счётчики?", node("p", {}, "Использованный суточный лимит станет доступен снова. История вызовов и минутное ограничение сохранятся. Это не меняет баланс у провайдера."), "Сбросить счётчики", async () => {
        await api("/settings/provider/reset", { method: "POST", body: { confirm: true } });
        notify("Начат новый суточный отсчёт. История сохранена."); if (route().section === "settings") render();
      }), { disabled: blocked }), node("a", { class: "button quiet", href: "#history?audit=1" }, "История изменений")));
}

async function historyView(query) {
  const pending = query.get("pending") === "1";
  const audit = query.get("audit") === "1";
  const offset = Math.max(0, Number(query.get("offset")) || 0);
  const selectedId = query.get("run");
  const filter = node("div", { class: "filters" },
    node("label", { class: "check-label" }, node("input", { type: "checkbox", checked: pending, disabled: audit, onchange: (event) => { location.hash = `history?pending=${event.target.checked ? "1" : "0"}`; } }), "Требуют проверки"),
    node("a", { class: "button", href: audit ? "#history" : "#history?audit=1" }, audit ? "Результаты обработки" : "Журнал действий"));
  const records = await api(`${audit ? "/audit" : pending ? "/reviews" : "/history"}?limit=100&offset=${offset}`);
  const content = node("div", {}, heading("История", audit ? "Журнал действий и результатов обработки." : "Сохранённые ответы, итоги и вопросы для уточнения."), filter);
  if (selectedId && !audit) {
    const selected = await api(`/qa/${encodeURIComponent(selectedId)}`);
    content.append(node("section", { class: "timeline", "aria-label": "Выбранный результат" }, runCard(selected), selected.conversation_id ? node("div", { class: "actions" }, node("a", { href: `#dialogs/${selected.conversation_id}?run=${selected.id}`, class: "button" }, "Открыть диалог"), button("Экспорт диалога", () => exportConversation(selected.conversation_id))) : null));
  }
  content.append(node("div", { class: "record-list" }, records.length ? records.map((item) => audit ? node("article", { class: "card audit-record" }, node("strong", {}, actions[item.action] || "Действие приложения"), node("p", { class: "record-meta" }, `${date(item.created_at)} · ${item.duration_ms} мс`), pill(item.status === "ok" ? "Записано" : "Ошибка", item.status === "ok" ? "neutral" : "error"),
    item.error ? node("p", { class: "small" }, reason(item.error)) : null,
    item.actor_id ? node("p", { class: "small" }, `Выполнил: ${item.actor_type === "api_key" ? "Сервисный API" : item.actor_id}`) : null,
    item.details?.previous_usage ? node("p", { class: "small" }, `До сброса: ${item.details.previous_usage.calls_per_day} вызовов, резерв ${item.details.previous_usage.token_budget_per_day} токенов.`) : null,
    item.details?.limits ? node("p", { class: "small" }, `Лимиты: ${item.details.limits.calls_per_minute} за минуту, ${item.details.limits.calls_per_day} за сутки, резерв ${item.details.limits.token_budget_per_day} токенов.`) : null,
    node("details", {}, node("summary", {}, "Технические сведения"), node("p", { class: "small muted" }, `Запрос: ${item.request_id || "—"}`)),
    item.conversation_id ? node("a", { href: `#dialogs/${item.conversation_id}`, class: "button" }, "Открыть диалог") : null) :
    node("article", { class: "card" }, node("div", { class: "record-heading" }, node("h2", {}, item.kind === "summary" ? "Итог беседы" : item.question), pill(item.reviewed_at ? "Проверка отмечена" : item.needs_review ? "Нужна проверка" : item.state === "running" ? "Выполняется…" : "Сохранено", item.needs_review && !item.reviewed_at ? "warning" : "neutral")), node("p", { class: "record-meta" }, date(item.created_at)),
      node("a", { class: "button", href: `#history?run=${item.id}${pending ? "&pending=1" : ""}` }, "Открыть результат"))) : node("div", { class: "card" }, empty(pending ? "Нет ответов в очереди проверки" : "Здесь появится история", pending ? "Новые вопросы не закрывают прежние проверки автоматически." : "Результаты сохраняются после обработки вопросов."))));
  content.append(pagination("history", records.length, offset, audit ? { audit: "1" } : pending ? { pending: "1" } : {}));
  return content;
}
async function render() {
  const version = ++state.sequence;
  const current = route();
  const view = $("#view");
  view.setAttribute("aria-busy", "true");
  view.inert = true;
  document.querySelectorAll("[data-nav]").forEach((link) => link.setAttribute("aria-current", link.dataset.nav === current.section ? "page" : "false"));
  try {
    const result = current.section === "documents" ? await documentsView(current.id, current.query) : current.section === "history" ? await historyView(current.query) : current.section === "settings" ? await settingsView() : await dialogsView(current.id, current.query);
    if (version !== state.sequence) return;
    view.replaceChildren(result); view.setAttribute("aria-busy", "false"); view.inert = false;
    const anchor = current.query.get("run");
    if (anchor && current.section === "dialogs") document.getElementById(`run-${anchor}`)?.scrollIntoView({ block: "start" });
  } catch (error) {
    if (version !== state.sequence) return;
    view.replaceChildren(node("div", { class: "card" }, empty("Не удалось открыть раздел", "Ваши данные не удалены. Проверьте соединение или обновите страницу."), button("Повторить загрузку", render)));
    view.setAttribute("aria-busy", "false"); view.inert = false; showError(error);
  }
}
if (document.body.dataset.page === "login") {
  $("#login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target; const submit = form.querySelector("button");
    if (submit.disabled) return;
    submit.disabled = true; submit.textContent = "Входим…"; $("#login-error").hidden = true;
    try { await api("/auth/login", { method: "POST", body: { username: form.username.value, password: form.password.value } }); form.password.value = ""; location.href = "/"; }
    catch (error) { $("#login-error").textContent = error.message; $("#login-error").hidden = false; }
    finally { submit.disabled = false; submit.textContent = "Войти"; }
  });
} else {
  $("#logout").addEventListener("click", async () => {
    const control = $("#logout"); control.disabled = true;
    try { await api("/auth/logout", { method: "POST" }); state.drafts.clear(); location.href = "/"; }
    catch (error) { showError(error); control.disabled = false; }
  });
  window.addEventListener("hashchange", () => { notify(""); render(); });
  render();
}
