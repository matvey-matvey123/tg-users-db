"use strict";

const DATA_URL = "data/users.json";

let database = [];
let lastResults = [];

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function normalize(v) {
  return String(v == null ? "" : v).toLowerCase().replace(/^@/, "").trim();
}

function matches(user, query) {
  const q = normalize(query);
  if (!q) return false;
  const username = normalize(user.username);
  const name = normalize(user.name);
  if (user.id != null && String(user.id) === q) return true;
  if (username === q) return true;
  if (user.usernames && user.usernames.some((u) => normalize(u.value) === q)) return true;
  if (user.names && user.names.some((n) => normalize(n.value).includes(q))) return true;
  if (username.includes(q)) return true;
  if (name.includes(q)) return true;
  if (user.phone && normalize(user.phone).includes(q)) return true;
  return false;
}

function renderHistoryBlock(title, rows, current) {
  if (!rows || rows.length === 0) return "";
  let body = "";
  for (const r of rows) {
    const isCurrent = current && normalize(r.value) === normalize(current);
    body += `<tr><td class="value">${escapeHtml(r.value)}${isCurrent ? ' <span class="badge">сейчас</span>' : ""}</td><td class="period">${escapeHtml(r.period || "")}</td></tr>`;
  }
  return `<div class="history-block"><h4>${escapeHtml(title)}</h4><table>${body}</table></div>`;
}

function tagsHtml(tags) {
  if (!tags || tags.length === 0) return "";
  return `<div>${tags.map((t) => `<field>${escapeHtml(t)}</field>`).join("")}</div>`;
}

function linksHtml(links) {
  if (!links || links.length === 0) return "";
  return `<div class="info-row"><span class="k">Ссылки</span><span class="v">${links.map((l) => `<a href="${escapeHtml(l)}" target="_blank" rel="noopener">${escapeHtml(l)}</a>`).join("<br>")}</span></div>`;
}

function infoRow(k, v) {
  if (!v) return "";
  return `<div class="info-row"><span class="k">${escapeHtml(k)}</span><span class="v">${escapeHtml(v)}</span></div>`;
}

function avatarUrl(username) {
  if (!username) return null;
  return `https://t.me/i/userpic/320/${username}.jpg`;
}

function userCard(user) {
  const avatar = avatarUrl(user.username);
  const img = avatar
    ? `<img src="${avatar}" width="56" height="56" alt="avatar" onerror="this.style.display='none'" style="border-radius:50%;">`
    : "";
  return `<h2>${img} ${escapeHtml(user.name || "Без имени")} <span class="badge">@${escapeHtml(user.username || "—")}</span></h2>
    <div class="meta">
      ${user.id != null ? `<span>ID: <a href="https://t.me/id/${escapeHtml(user.id)}">${escapeHtml(user.id)}</a></span>` : ""}
      <span>Первый раз замечен: ${escapeHtml(user.first_seen || "—")}</span>
      <span>Последний раз: ${escapeHtml(user.last_seen || "—")}</span>
    </div>
    ${user.saved_as ? infoRow("Как записано у тебя", user.saved_as) : ""}
    ${renderHistoryBlock("История юзернеймов", user.usernames, user.username)}
    ${renderHistoryBlock("История имён", user.names, user.name)}
    ${tagsHtml(user.tags)}
    ${infoRow("Био", user.bio)}
    ${infoRow("Телефон", user.phone)}
    ${linksHtml(user.links)}
    ${infoRow("Заметки", user.notes)}`;
}

function listRow(user, i) {
  return `<div class="user-row" onclick="showUser(${i})">
    <div class="row-name">${escapeHtml(user.name || user.username || "Без имени")}</div>
    <div class="row-username">${user.username ? "@" + escapeHtml(user.username) : ""}</div>
    <div class="row-meta">${user.id != null ? "ID: " + escapeHtml(user.id) : ""}</div>
    ${(user.tags && user.tags.length) ? `<div class="row-tags">${user.tags.map((t) => `<field>${escapeHtml(t)}</field>`).join("")}</div>` : ""}
  </div>`;
}

function showUser(i) {
  const user = lastResults[i];
  if (!user) return;
  const card = document.getElementById("modal-card");
  card.innerHTML = userCard(user);
  document.getElementById("modal").classList.remove("hidden");
  document.body.style.overflow = "hidden";
}

function closeModal() {
  document.getElementById("modal").classList.add("hidden");
  document.body.style.overflow = "";
}

function showResults(users, query) {
  lastResults = users;
  const container = document.getElementById("results");
  const status = document.getElementById("status");

  if (users.length === 0) {
    status.textContent = `По запросу «${query}» ничего не найдено. Попробуйте другой юзернейм или ID.`;
    container.innerHTML = "";
    return;
  }

  status.textContent = `Найдено: ${users.length}`;
  container.innerHTML = users.map(listRow).join("");
}

async function loadDatabase() {
  const res = await fetch(DATA_URL, { cache: "no-cache" });
  if (!res.ok) throw new Error(`Не удалось загрузить базу (HTTP ${res.status})`);
  const data = await res.json();
  database = data.users || [];
}

function runSearch(query) {
  if (!query.trim()) {
    document.getElementById("status").textContent = "Введите юзернейм, имя или ID.";
    document.getElementById("results").innerHTML = "";
    return;
  }
  showResults(database.filter((u) => matches(u, query)), query.trim());
}

document.addEventListener("DOMContentLoaded", async () => {
  const input = document.getElementById("search-input");
  const button = document.getElementById("search-button");
  const status = document.getElementById("status");

  status.textContent = "Загружаю базу…";
  try {
    await loadDatabase();
    status.textContent = `База загружена: ${database.length} записей.`;
  } catch (e) {
    status.textContent = "Ошибка загрузки базы: " + e.message;
  }

  button.addEventListener("click", () => runSearch(input.value));
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(input.value); });
  input.addEventListener("input", () => { if (input.value.trim()) runSearch(input.value); });

  document.getElementById("modal").addEventListener("click", (e) => {
    if (e.target.id === "modal") closeModal();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
});