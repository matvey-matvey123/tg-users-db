"use strict";

function parseLines(text) {
  return (text || "")
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .map((l) => {
      const i = l.indexOf("|");
      if (i === -1) return { value: l, period: "" };
      return { value: l.slice(0, i).trim(), period: l.slice(i + 1).trim() };
    });
}

function parseCommas(text) {
  return (text || "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

function parseLinks(text) {
  return (text || "")
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
}

function buildJson() {
  const val = (id) => document.getElementById(id).value.trim();
  const idVal = val("f-id");
  const user = {
    id: idVal ? parseInt(idVal, 10) || null : null,
    username: val("f-username"),
    name: val("f-name"),
  };
  if (val("f-first_seen")) user.first_seen = val("f-first_seen");
  if (val("f-last_seen")) user.last_seen = val("f-last_seen");

  const usernames = parseLines(val("f-usernames"));
  if (usernames.length) user.usernames = usernames;

  const names = parseLines(val("f-names"));
  if (names.length) user.names = names;

  if (val("f-bio")) user.bio = val("f-bio");
  if (val("f-phone")) user.phone = val("f-phone");

  const tags = parseCommas(val("f-tags"));
  if (tags.length) user.tags = tags;

  const links = parseLinks(val("f-links"));
  if (links.length) user.links = links;

  if (val("f-notes")) user.notes = val("f-notes");

  return JSON.stringify(user, null, 2);
}

document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("add-form");
  const output = document.getElementById("output");
  const copyButton = document.getElementById("copy-button");

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    output.textContent = buildJson();
    output.classList.remove("hidden");
    copyButton.classList.remove("hidden");
  });

  copyButton.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(output.textContent);
      copyButton.textContent = "Скопировано!";
      setTimeout(() => { copyButton.textContent = "Скопировать JSON"; }, 1500);
    } catch (_) {
      output.select();
      document.execCommand("copy");
    }
  });
});