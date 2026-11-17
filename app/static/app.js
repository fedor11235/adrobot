"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const state = {campaigns: [], selected: null, campaign: null, config: {}, busy: false};
const colors = ["#74964e", "#b8ce91", "#d7e5bc", "#98b477", "#c3d5ac", "#a0b391"];
const pinIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="m15 3 6 6-3 1-4 5v3l-2 2-3-5-5-3 2-2h3l5-4 1-3Z"/><path d="m8 16-5 5"/></svg>';
let toastTimer;

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {...options, headers: {"Content-Type": "application/json", ...options.headers}});
  let data;
  try { data = await response.json(); } catch { throw new Error("Сервер вернул неожиданный ответ."); }
  if (!response.ok) {
    const details = data.errors?.map(e => `${e.field}: ${e.message}`).join("; ");
    const error = new Error(`${data.detail || "Не удалось выполнить запрос."}${details ? ` ${details}` : ""}`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function notify(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("visible"), 4000);
}

function errorAt(selector, error) {
  const el = $(selector);
  el.textContent = error?.message || "";
  el.hidden = !error;
}

async function working(task, errorSelector = "#page-error") {
  if (state.busy) return;
  state.busy = true;
  document.body.classList.add("busy");
  $("#busy-indicator").hidden = false;
  $("#workspace").setAttribute("aria-busy", "true");
  const controls = [...document.querySelectorAll("button,input,select")].filter(control => !control.disabled);
  for (const control of controls) control.disabled = true;
  errorAt(errorSelector, null);
  try { return await task(); }
  catch (error) { errorAt(errorSelector, error); }
  finally {
    state.busy = false;
    document.body.classList.remove("busy");
    $("#busy-indicator").hidden = true;
    $("#workspace").setAttribute("aria-busy", "false");
    for (const control of controls) if (control.isConnected) control.disabled = false;
  }
}

function renderCampaigns() {
  const query = $("#campaign-search").value.trim().toLocaleLowerCase();
  const campaigns = state.campaigns.filter(c => `${c.id} ${c.name}`.toLocaleLowerCase().includes(query));
  $("#campaign-count").textContent = String(state.campaigns.length).padStart(2, "0");
  $("#campaign-list").innerHTML = campaigns.length ? campaigns.map(c => `
    <button class="campaign-item ${c.id === state.selected ? "selected" : ""}" data-campaign="${c.id}" ${c.id === state.selected ? 'aria-current="true"' : ""}>
      <span class="campaign-name">${escapeHtml(c.name)}</span><span class="campaign-id">CAMPAIGN / ${c.id}</span>
    </button>`).join("") : '<p class="side-empty">Кампаний не найдено</p>';
}

function setCampaign(campaign) {
  state.campaign = campaign;
  state.selected = campaign.id;
  localStorage.setItem(`adrobot-${state.config.mode}-campaign`, campaign.id);
  renderCampaigns();
  renderWorkspace();
}

async function loadCampaign(id) {
  let campaign;
  try { campaign = await api(`/campaigns/${id}`); }
  catch (error) {
    if (error.status !== 404) throw error;
    campaign = await api(`/campaigns/${id}/fetch`, {method: "POST"});
  }
  setCampaign(campaign);
}

function dateLabel(value) {
  const date = new Date(/[Z+]\d*:?\d*$/.test(value) ? value : `${value}Z`);
  return date.toLocaleTimeString("ru-RU", {hour: "2-digit", minute: "2-digit"});
}

function renderWorkspace() {
  const c = state.campaign;
  if (!c) return;
  const drafts = c.streams.filter(s => s.dirty).length;
  const count = c.streams.reduce((sum, s) => sum + s.active_count, 0);
  $("#campaign-content").innerHTML = `
    <div class="campaign-heading"><div><p class="eyebrow">ОБЗОР КАМПАНИИ</p><h1>${escapeHtml(c.name)}</h1>
      <div class="campaign-meta"><span class="mono">ID ${c.id}</span><span class="meta-dot">·</span><span>${escapeHtml(c.alias)}</span></div></div>
      <button class="button secondary" id="fetch-streams"><span aria-hidden="true">↻</span> Fetch Streams</button>
    </div>
    <div class="overview"><div class="stat"><strong>${String(c.streams.length).padStart(2,"0")}</strong><span>потоков</span></div>
      <div class="stat"><strong>${String(count).padStart(2,"0")}</strong><span>активных офферов</span></div>
      <div class="stat"><strong>${String(drafts).padStart(2,"0")}</strong><span>черновиков</span></div></div>
    <div class="section-heading"><h2>Потоки кампании</h2><p>Изменения появятся в Keitaro после публикации</p></div>
    <div id="streams">${c.streams.map((s, i) => renderStream(s, i)).join("") || '<p class="notice">В этой кампании пока нет потоков.</p>'}</div>`;
  for (const stream of c.streams) {
    if (stream.editable && !stream.remote_missing) makePicker($(`#picker-${stream.id}`), `offer-${stream.id}`, "Название или ID оффера…");
  }
}

function renderStream(s, index) {
  const editable = s.editable && !s.remote_missing;
  const active = s.offers.filter(o => !o.removed && o.remote_state === "active");
  const status = s.remote_missing ? "Удалён в Keitaro" : s.dirty ? "Не опубликован" : "Синхронизирован";
  const body = editable ? `<div class="stream-body">
    <div class="distribution" role="img" aria-label="Распределение трафика: ${active.map(o => `${escapeHtml(o.name)} ${o.share}%`).join(", ")}">${active.map((o, i) => `<span style="width:${o.share}%;background:${colors[i % colors.length]}" title="${escapeHtml(o.name)}: ${o.share}%"></span>`).join("")}</div>
    <div class="distribution-info"><span>РАСПРЕДЕЛЕНИЕ ТРАФИКА</span><span class="${s.total !== 100 ? "invalid" : ""}">${s.total}% из 100%</span></div>
    <div class="notice error stream-error" id="error-${s.id}" role="alert" hidden></div>
    <table class="offers-table"><caption class="sr-only">Офферы потока ${escapeHtml(s.name)}</caption><thead><tr><th scope="col">ОФФЕР</th><th scope="col">ДОЛЯ</th><th scope="col"><span class="sr-only">Закрепление доли</span>ФИКС.</th><th scope="col"><span class="sr-only">Действия</span></th></tr></thead>
    <tbody>${s.offers.map((o, i) => renderOffer(s, o, i)).join("")}</tbody></table>
    <div class="stream-add"><div id="picker-${s.id}"></div><button class="button secondary" data-action="add" data-stream="${s.id}"><span aria-hidden="true">＋</span> Добавить</button></div></div>`
    : `<div class="readonly-body">${s.remote_missing ? "Поток отсутствует в Keitaro. Локальная история сохранена." : "Поток без офферов. Его настройки остаются в Keitaro."}</div>`;
  const footer = (s.editable || s.dirty) ? `<div class="stream-footer"><p class="footer-hint">${s.dirty ? "Черновик сохранён. Keitaro пока без изменений." : `Последняя синхронизация в ${dateLabel(s.synced_at)}`}</p>
    <div class="stream-actions">${editable && s.total !== 100 ? `<button class="text-button" data-action="balance" data-stream="${s.id}">Выровнять веса</button>` : ""}
    <button class="text-button" data-action="cancel" data-stream="${s.id}" ${!s.dirty ? "disabled" : ""}>Отменить</button>
    <button class="button primary" data-action="publish" data-stream="${s.id}" ${!s.dirty || !editable || !active.length || s.total !== 100 ? "disabled" : ""}>В Keitaro <span aria-hidden="true">↗</span></button></div></div>` : "";
  return `<article class="stream-card ${s.dirty ? "dirty" : ""} ${s.remote_missing ? "missing" : ""}" data-stream-card="${s.id}"><header class="stream-header"><div class="stream-title"><span class="stream-number">${String(index + 1).padStart(2,"0")}</span><div><h3>${escapeHtml(s.name)}</h3><p class="stream-sub">STREAM / ${s.id}</p></div></div><span class="badge ${s.remote_missing ? "gray" : s.dirty ? "draft" : ""}">${status}</span></header>${body}${footer}</article>`;
}

function renderOffer(s, o, index) {
  const disabled = o.removed || o.remote_state !== "active";
  return `<tr class="${disabled ? "removed" : ""}"><td><div class="offer-description"><span class="offer-dot" style="background:${colors[index % colors.length]}" aria-hidden="true"></span><div><p class="offer-name">${escapeHtml(o.name)}${disabled ? `<span class="removed-tag">${o.removed ? "В АРХИВЕ" : "ПРИОСТАНОВЛЕН"}</span>` : ""}</p><p class="offer-id">#${o.offer_id}</p></div></div></td>
    <td><div class="share-control"><input type="number" min="0" max="100" step="1" value="${o.share}" class="${o.pinned ? "pinned" : ""}" aria-label="Доля оффера ${o.offer_id}" data-share="${o.offer_id}" data-stream="${s.id}" ${disabled || !o.pinned ? "disabled" : ""}><span>%</span></div></td>
    <td><button class="pin-button ${o.pinned ? "active" : ""}" data-action="pin" data-stream="${s.id}" data-offer="${o.offer_id}" aria-pressed="${o.pinned}" aria-label="${o.pinned ? "Открепить" : "Закрепить"} долю оффера ${o.offer_id}" ${disabled ? "disabled" : ""}>${pinIcon}</button></td>
    <td><button class="row-action ${o.removed ? "restore" : ""}" data-action="${o.removed ? "restore" : "remove"}" data-stream="${s.id}" data-offer="${o.offer_id}" aria-label="${o.removed ? "Вернуть" : "Удалить"} оффер ${o.offer_id}">${o.removed ? "Вернуть ↶" : "Удалить ×"}</button></td></tr>`;
}

function makePicker(root, id, placeholder) {
  delete root.dataset.offer;
  root.className = "picker";
  root.innerHTML = `<input id="${id}" type="search" placeholder="${placeholder}" autocomplete="off" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="${id}-results" aria-label="Поиск оффера"><div id="${id}-results" class="picker-results" role="listbox" aria-label="Найденные офферы" hidden></div><div class="selected-offer" aria-live="polite"></div>`;
  const input = $("input", root), results = $(".picker-results", root);
  let timer, generation = 0;
  function close() { results.hidden = true; input.setAttribute("aria-expanded", "false"); }
  input.addEventListener("input", () => {
    delete root.dataset.offer;
    $(".selected-offer", root).textContent = "";
    clearTimeout(timer);
    const local = ++generation;
    if (!input.value.trim()) return close();
    timer = setTimeout(async () => {
      try {
        const entries = await api(`/offers?q=${encodeURIComponent(input.value)}`);
        if (local !== generation || !root.isConnected) return;
        results.innerHTML = entries.length ? entries.map(o => `<button type="button" class="picker-option" role="option" data-id="${o.id}" data-name="${escapeHtml(o.name)}"><span>#${o.id}</span>${escapeHtml(o.name)}</button>`).join("") : '<div class="picker-note">Офферы не найдены</div>';
        results.hidden = false;
        input.setAttribute("aria-expanded", "true");
      } catch (error) {
        if (local !== generation || !root.isConnected) return;
        results.innerHTML = `<div class="picker-note">${escapeHtml(error.message)}</div>`;
        results.hidden = false;
        input.setAttribute("aria-expanded", "true");
      }
    }, 220);
  });
  results.addEventListener("click", event => {
    const option = event.target.closest(".picker-option");
    if (!option) return;
    root.dataset.offer = option.dataset.id;
    input.value = option.dataset.name;
    $(".selected-offer", root).textContent = `Выбран оффер #${option.dataset.id}`;
    close(); input.focus();
  });
  root.addEventListener("keydown", event => {
    if (event.key === "Escape") { event.stopPropagation(); close(); input.focus(); }
    if (event.key === "ArrowDown" && !results.hidden) { event.preventDefault(); const all = [...results.querySelectorAll("button")]; all[(all.indexOf(document.activeElement) + 1) % all.length]?.focus(); }
    if (event.key === "ArrowUp" && !results.hidden) { event.preventDefault(); const all = [...results.querySelectorAll("button")]; all[(all.indexOf(document.activeElement) - 1 + all.length) % all.length]?.focus(); }
  });
  root.addEventListener("focusout", () => setTimeout(() => { if (!root.contains(document.activeElement)) close(); }, 100));
}

async function mutate(streamId, action, extra = {}, method = "POST") {
  const stream = state.campaign.streams.find(s => s.id === streamId);
  const updated = await api(`/streams/${streamId}/${action}`, {method, body: JSON.stringify({revision: stream.revision, ...extra})});
  state.campaign.streams = state.campaign.streams.map(s => s.id === streamId ? updated : s);
  renderWorkspace();
}

document.addEventListener("click", event => {
  const action = event.target.closest("[data-action]");
  if (action && !action.disabled && !state.busy) {
    const streamId = Number(action.dataset.stream), offerId = Number(action.dataset.offer);
    const stream = state.campaign.streams.find(s => s.id === streamId);
    const errorSelector = $(`#error-${streamId}`) ? `#error-${streamId}` : "#page-error";
    working(async () => {
      switch (action.dataset.action) {
        case "add": {
          const selected = $(`#picker-${streamId}`).dataset.offer;
          if (!selected) throw new Error("Найдите оффер и выберите его из списка.");
          await mutate(streamId, "offers", {offer_id: Number(selected)}); break;
        }
        case "pin": {
          const offer = stream.offers.find(o => o.offer_id === offerId);
          await mutate(streamId, `offers/${offerId}`, {pinned: !offer.pinned}, "PATCH"); break;
        }
        case "remove": case "restore":
          await mutate(streamId, `offers/${offerId}/${action.dataset.action}`); break;
        case "publish":
          await mutate(streamId, "publish"); notify("Изменения опубликованы в Keitaro"); break;
        case "cancel":
          await mutate(streamId, "cancel"); notify("Черновик отменён"); break;
        case "balance": await mutate(streamId, "balance"); break;
      }
    }, errorSelector);
  }
  const campaign = event.target.closest("[data-campaign]");
  if (campaign && !state.busy) working(() => loadCampaign(Number(campaign.dataset.campaign)));
  if (event.target.closest("#fetch-streams") && !state.busy) working(async () => {
    const updated = await api(`/campaigns/${state.selected}/fetch`, {method:"POST"});
    setCampaign(updated); notify("Потоки загружены. Локальные черновики сохранены.");
  });
});

document.addEventListener("change", event => {
  const input = event.target.closest("[data-share]");
  if (!input || state.busy) return;
  const streamId = Number(input.dataset.stream), offerId = Number(input.dataset.share);
  const previous = state.campaign.streams.find(s => s.id === streamId).offers.find(o => o.offer_id === offerId).share;
  working(async () => {
    try {
      const value = Number(input.value);
      if (!input.value || !Number.isInteger(value) || value < 0 || value > 100) throw new Error("Доля должна быть целым числом от 0 до 100.");
      await mutate(streamId, `offers/${offerId}`, {pinned: true, share: value}, "PATCH");
    } catch (error) { input.value = previous; throw error; }
  }, `#error-${streamId}`);
});

$("#campaign-search").addEventListener("input", renderCampaigns);
$("#new-campaign").addEventListener("click", () => working(async () => {
  const lookups = await api("/lookups");
  const form = $("#create-form"); form.reset();
  makePicker($("#create-picker"), "create-offer", "Название или ID…");
  for (const [field, kind] of [["domain_id","domains"],["group_id","groups"],["source_id","sources"]]) {
    const select = form.elements[field];
    select.innerHTML = lookups[kind].map(x => `<option value="${x.id}">${escapeHtml(x.name)}</option>`).join("");
    if (state.config.defaults?.[field]) select.value = state.config.defaults[field];
  }
  errorAt("#form-error", null);
  $("#create-dialog").showModal();
}));
for (const id of ["#close-dialog", "#cancel-dialog"]) $(id).addEventListener("click", () => { if (!state.busy) $("#create-dialog").close(); });
$("#create-dialog").addEventListener("cancel", event => { if (state.busy) event.preventDefault(); });
$("#create-form").addEventListener("submit", event => {
  event.preventDefault();
  if (state.busy) return;
  const submitted = Object.fromEntries(new FormData(event.target));
  working(async () => {
    const offer = $("#create-picker").dataset.offer;
    if (!offer) throw new Error("Выберите оффер из результатов поиска.");
    const data = {...submitted};
    for (const field of ["domain_id", "group_id", "source_id"]) data[field] = Number(data[field]);
    data.offer_id = Number(offer);
    const campaign = await api("/campaigns", {method:"POST", body:JSON.stringify(data)});
    state.campaigns = [{id:campaign.id, name:campaign.name, alias:campaign.alias}, ...state.campaigns];
    setCampaign(campaign); $("#create-dialog").close(); notify("Кампания создана в Keitaro");
  }, "#form-error");
});

working(async () => {
  state.config = await api("/config");
  $("#demo-banner").hidden = state.config.mode !== "demo";
  $("#mode-label").textContent = state.config.mode === "demo" ? "DEMO WORKSPACE" : "KEITARO WORKSPACE";
  state.campaigns = await api("/campaigns");
  $("#connection-label").textContent = state.config.mode === "demo" ? "Демотрекер подключён" : "Keitaro подключён";
  $("#connection-dot").classList.add("connected");
  renderCampaigns();
  const remembered = Number(localStorage.getItem(`adrobot-${state.config.mode}-campaign`));
  const first = state.campaigns.find(c => c.id === remembered) || state.campaigns[0];
  if (first) await loadCampaign(first.id);
});
