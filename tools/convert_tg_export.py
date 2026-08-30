#!/usr/bin/env python3
"""Конвертер экспорта контактов Telegram Desktop в data/users.json сайта.

Умеет читать:
  - contacts.json из экспорта Telegram Desktop (структура с ключом "users"/"contacts");
  - любой JSON: объект или список со списками пользователей внутри;
  - lists/contacts.html из экспорта Telegram Desktop (имя из телефонной книги + телефон).

Использование:
  python convert_tg_export.py path/to/contacts.json [--output data/users.json]
  python convert_tg_export.py path/to/contacts.html --output data/users.json
  python convert_tg_export.py contacts.json --merge-existing data/users.json
  python convert_tg_export.py contacts.json --no-phone --no-bio --tag "контакт"

Группы, каналы (id < 0) и удалённые аккаунты отбрасываются автоматически.
"""

import argparse
import html.parser
import json
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

PLACEHOLDER_NAMES = {
    "deleted account",
    "удалённый аккаунт",
    "deleted",
    "удалён",
}

FIELD_ALIASES = {
    "id": ["id", "user_id"],
    "first_name": ["first_name", "fn"],
    "last_name": ["last_name", "ln"],
    "username": ["username", "user_name", "nickname"],
    "phone": ["phone", "phone_number"],
    "bio": ["bio", "about"],
    "date": ["date", "last_seen_date", "registered"],
}


def get_field(item, key):
    for alias in FIELD_ALIASES[key]:
        if alias in item and item[alias] is not None:
            return item[alias]
    return None


def normalize_phone(value):
    v = clean(value)
    if v.startswith("00"):
        v = "+" + v[2:]
    return v


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def looks_like_user(item):
    if not isinstance(item, dict):
        return False
    rec_id = get_field(item, "id")
    if rec_id is not None:
        try:
            rec_id = int(rec_id)
        except (TypeError, ValueError):
            rec_id = None
        if rec_id is not None and rec_id <= 0:
            return False
    has = any(get_field(item, k) for k in ("first_name", "username", "phone"))
    return has


def extract_user_lists(data):
    """Возвращает список списков записей-пользователей из любого JSON."""
    lists = []

    def walk(node):
        if isinstance(node, dict):
            for key in ("users", "contacts"):
                if key in node and isinstance(node[key], list):
                    items = [i for i in node[key] if looks_like_user(i)]
                    if items:
                        lists.append(items)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            items = [i for i in node if looks_like_user(i)]
            if items:
                lists.append(items)
            for value in node:
                walk(value)

    walk(data)

    seen = set()
    unique = []
    for lst in lists:
        key = tuple(sorted((u.get("id") for u in lst if u.get("id") is not None)))
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        unique.append(lst)
    return unique if unique else ([data] if looks_like_user(data) else [])


class ContactsHTMLParser(html.parser.HTMLParser):
    """Разбирает lists/contacts.html из экспорта Telegram Desktop."""

    def __init__(self):
        super().__init__()
        self.stack = []
        self.current = None
        self.entries = []

    def handle_starttag(self, tag, attrs):
        cls = None
        for key, value in attrs:
            if key == "class":
                cls = value or None
        self.stack.append(cls)
        if tag == "div" and cls and "entry" in cls.split() and self.current is None:
            self.current = {}

    def handle_endtag(self, tag):
        was = self.stack.pop() if self.stack else None
        if tag == "div" and was and "entry" in was.split() and self.current is not None:
            entry = self.current
            self.current = None
            if entry.get("name") or entry.get("phone"):
                self.entries.append(entry)

    def handle_data(self, data):
        if self.current is None or not self.stack:
            return
        classes = self.stack[-1].split() if self.stack[-1] else []
        text = data.strip()
        if not text:
            return
        if "name" in classes and "bold" in classes:
            self.current.setdefault("name", text)
        elif "details_entry" in classes:
            self.current.setdefault("phone", text)


def parse_contacts_html(path):
    parser = ContactsHTMLParser()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser.entries


def convert_entry(item):
    """Превращает сырую запись (JSON или HTML) в запись базы сайта."""
    rec_id = get_field(item, "id")
    if rec_id is not None:
        try:
            rec_id = int(rec_id)
        except (TypeError, ValueError):
            rec_id = None
        if rec_id is not None and rec_id <= 0:
            return None

    first = clean(get_field(item, "first_name"))
    last = clean(get_field(item, "last_name"))
    raw_name = clean(item.get("name")) if isinstance(item.get("name"), str) else ""
    name = raw_name or " ".join(filter(None, [first, last]))

    if name.lower() in PLACEHOLDER_NAMES:
        return None

    user = {}
    if rec_id:
        user["id"] = rec_id

    username = clean(get_field(item, "username"))
    if username:
        user["username"] = username.lstrip("@")
    if name:
        user["name"] = name

    phone = normalize_phone(get_field(item, "phone"))
    if phone and phone.isdigit():
        phone = "+" + phone
    if phone:
        user["phone"] = phone

    bio = clean(get_field(item, "bio"))
    if bio:
        user["bio"] = bio

    date = clean(get_field(item, "date"))
    if date and ":" not in date:
        user["first_seen"] = date.split("T")[0][:10]

    return user


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", help="путь к contacts.json/contacts.html (или любому JSON экспорта)")
    ap.add_argument("-o", "--output", default="data/users.json",
                    help="куда записать результат (по умолчанию data/users.json)")
    ap.add_argument("--merge-existing", default=None,
                    help="объединить с уже существующим файлом базы (без дубликатов)")
    ap.add_argument("--no-phone", action="store_true", help="не включать телефоны в вывод")
    ap.add_argument("--no-bio", action="store_true", help="не включать био в вывод")
    ap.add_argument("--tag", action="append", default=[],
                    help="добавить тег (можно несколько раз), по умолчанию добавляется 'контакт'")
    ap.add_argument("--limit", type=int, default=0,
                    help="включить только первые N записей (для проверки)")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"Ошибка: файл не найден: {src}")

    tags = list(args.tag) + (["контакт"] if "контакт" not in args.tag else [])

    if src.suffix.lower() == ".html":
        entries = parse_contacts_html(src)
    else:
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
        except Exception as e:
            sys.exit(f"Ошибка чтения JSON: {e}")
        lists = extract_user_lists(data)
        if not lists:
            sys.exit("В файле не найдено записей-контактов.")
        entries = [item for lst in lists for item in lst]

    contacts = []
    for item in entries:
        user = convert_entry(item)
        if not user:
            continue
        if args.no_phone:
            user.pop("phone", None)
        if args.no_bio:
            user.pop("bio", None)
        user["tags"] = tags
        contacts.append(user)

    if args.limit:
        contacts = contacts[: args.limit]

    if args.merge_existing:
        existing = Path(args.merge_existing)
        if existing.exists():
            old = json.loads(existing.read_text(encoding="utf-8")).get("users", [])
            known = {u.get("id") for u in old if u.get("id") is not None}
            merged = list(old)
            new = []
            for u in contacts:
                if u.get("id") is None or u.get("id") not in known:
                    pass
                if u.get("id") is not None and u.get("id") in known:
                    continue
                if u.get("phone"):
                    phone_match = any(str(o.get("phone", "")).replace("+", "").replace(" ", "")
                                      == str(u["phone"]).replace("+", "").replace(" ", "")
                                      for o in old)
                    if phone_match:
                        continue
                merged.append(u)
                new.append(u)
            contacts = merged

    contacts.sort(key=lambda u: clean(u.get("name") or u.get("username") or "").lower() + "|" + str(u.get("id") or ""))

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"users": contacts}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Готово: {len(contacts)} контактов записано в {out}")


if __name__ == "__main__":
    main()