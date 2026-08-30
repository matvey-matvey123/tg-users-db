#!/usr/bin/env python3
"""Забирает данные из личного аккаунта Telegram и пишет их в data/users.json сайта.

Требуется свой api_id и api_hash с https://my.telegram.org (-> API development tools).
Сессия сохраняется локально, при первом запуске запросит код из Telegram и пароль (если есть 2FA).

Примеры:
  python tools/tg_pull.py --api-id 12345678 --api-hash aaa...bbb          # контакты (био)
  python tools/tg_pull.py --api-id 123 --api-hash xxx --no-bio           # без био (быстрее)
  python tools/tg_pull.py --api-id 123 --api-hash xxx --include-chats    # ещё и диалоги людей
  python tools/tg_pull.py --api-id 123 --api-hash xxx --with-phone       # включить телефоны (публично!)
  python tools/tg_pull.py --api-id 123 --api-hash xxx --search @user1 username2  # поиск людей

Параметры можно задать переменными окружения: TG_API_ID, TG_API_HASH, TG_PHONE.
Данные не заменяют базу, а добавляются к существующим записям.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from telethon.network import (
    ConnectionTcpMTProxyIntermediate,
    ConnectionTcpMTProxyRandomizedIntermediate,
)
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.functions.users import GetFullUserRequest


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_phone(value):
    if type(value) is int:
        value = str(value)
    value = clean(value)
    if value.startswith("00"):
        value = "+" + value[2:]
    if value and value[0] != "+":
        value = "+" + value
    return value


def build_entry(user, with_phone, with_bio, tags, full=None):
    if user.bot:
        return None
    entry = {"id": user.id if hasattr(user, "id") else None}
    if getattr(user, "username", None):
        entry["username"] = user.username.lstrip("@")
    name = clean(" ".join(filter(None, [getattr(user, "first_name", ""), getattr(user, "last_name", "")])))
    if name:
        entry["name"] = name
    if with_phone and getattr(user, "phone", None):
        entry["phone"] = normalize_phone(user.phone)
    if with_bio and full is not None and getattr(full.full_user, "about", None):
        bio = clean(full.full_user.about)
        if bio:
            entry["bio"] = bio
    entry["tags"] = list(tags)
    return entry


async def load_db(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("users", [])
        except Exception:
            return []
    return []


def merge_users(existing, contacts):
    """Добавляет новые записи и обновляет авто-поля существующих (имя/юзернейм/био/телефон),
    сохраняя ручные правки (заметки, теги, историю).
    """
    by_id = {}
    by_phone = {}
    for u in existing:
        if u.get("id") is not None:
            by_id[u["id"]] = u
        if u.get("phone"):
            by_phone[u["phone"].replace("+", "").replace(" ", "")] = u

    AUTO = ("username", "name", "bio", "phone")
    added = updated = 0

    for c in contacts:
        cid = c.get("id")
        phone_key = (c.get("phone") or "").replace("+", "").replace(" ", "")
        if cid is not None and cid in by_id:
            tgt = by_id[cid]
            for k in AUTO:
                if k in c and tgt.get(k) != c[k]:
                    tgt[k] = c[k]
                    updated += 1
        elif phone_key and phone_key in by_phone:
            continue
        else:
            if cid is not None:
                by_id[cid] = c
            if phone_key:
                by_phone[phone_key] = c
            existing.append(c)
            added += 1
    return existing, added, updated


async def collect_contacts(client, tags, with_phone, with_bio, include_chats, limit):
    print("Загружаю список контактов…")
    contacts = await client(GetContactsRequest(hash=0))
    users = [u for u in contacts.users if not u.bot]

    if include_chats:
        print("Собираю личные диалоги…")
        seen = {u.id for u in users}
        async for dialog in client.iter_dialogs(limit=None):
            if dialog.is_user and not dialog.entity.bot and dialog.entity.id not in seen:
                seen.add(dialog.entity.id)
                users.append(dialog.entity)

    if limit:
        users = users[:limit]

    entries, errors = [], 0
    for i, u in enumerate(users, 1):
        full = None
        if with_bio:
            try:
                full = await client(GetFullUserRequest(u))
            except Exception as e:
                errors += 1
                if isinstance(e, FloodWaitError):
                    print(f"  флуд-лимит, жду {e.seconds} c…")
                    await asyncio.sleep(e.seconds + 1)
        entry = build_entry(u, with_phone, with_bio, tags, full)
        if entry:
            entries.append(entry)
        if i % 10 == 0:
            print(f"  обработано {i}/{len(users)}")
    if errors:
        print(f"Предупреждение: {errors} записей без био (не найдены/лимиты).")
    return entries


async def collect_search(client, queries, tags, with_phone, with_bio):
    entries = []
    for q in queries:
        try:
            u = await client.get_entity(q)
        except Exception as e:
            print(f"  {q}: не найден или ошибка ({e})")
            continue
        if u.bot:
            print(f"  {q}: это бот, пропускаю")
            continue
        full = None
        if with_bio:
            try:
                full = await client(GetFullUserRequest(u))
            except Exception:
                pass
        entry = build_entry(u, with_phone, with_bio, tags, full)
        if entry:
            entries.append(entry)
            print(f"  + {q} -> {entry.get('name') or entry.get('username') or entry.get('id')}")
    return entries


async def run(args):
    api_id = args.api_id or os.environ.get("TG_API_ID")
    api_hash = args.api_hash or os.environ.get("TG_API_HASH")
    phone = args.phone or os.environ.get("TG_PHONE")
    if not api_id or not api_hash:
        sys.exit("Укажи --api-id и --api-hash (или переменные TG_API_ID / TG_API_HASH)."
                 "\nПолучить: https://my.telegram.org -> API development tools")

    session = Path(args.session)
    session.parent.mkdir(parents=True, exist_ok=True)

    kwargs = {}
    secret = (args.proxy_secret or "").strip().lower()
    if args.proxy_host and secret:
        port = int(args.proxy_port or 443)
        if secret.startswith("ee"):
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import telethon_faketls
            kwargs.update(connection=telethon_faketls.ConnectionTcpMTProxyFakeTLS,
                          proxy=(args.proxy_host, port, secret[2:]))
        elif secret.startswith("dd"):
            kwargs.update(connection=ConnectionTcpMTProxyRandomizedIntermediate,
                          proxy=(args.proxy_host, port, secret[2:]))
        else:
            kwargs.update(connection=ConnectionTcpMTProxyIntermediate,
                          proxy=(args.proxy_host, port, secret))

    client = TelegramClient(str(session), int(api_id), api_hash, **kwargs)

    code = args.code or os.environ.get("TG_CODE", "")
    password = args.password or os.environ.get("TG_PASSWORD", "")

    try:
        await client.connect()
        if not await client.is_user_authorized():
            _phone = phone or input("Номер телефона: ")
            await client.send_code_request(_phone)
            _code = code or input("Код из Telegram: ")
            try:
                await client.sign_in(_phone, _code)
            except SessionPasswordNeededError:
                await client.sign_in(password=password or input("Пароль 2FA: "))
    except EOFError:
        sys.exit("Код из Telegram уже отправлен. Повтори запуск с TG_CODE=<код> "
                 "(и TG_PASSWORD=<пароль>, если включена 2FA).")
    except Exception as e:
        sys.exit(f"Ошибка входа: {e}")

    me = await client.get_me()
    print(f"Вошли как {me.first_name} (@{me.username}, id {me.id})")

    tags = list(args.tag) + (["контакт"] if not args.search else [])

    existing = [] if args.overwrite else await load_db(Path(args.output))
    if args.search:
        contacts = await collect_search(client, args.search, tags, args.with_phone, not args.no_bio)
    else:
        contacts = await collect_contacts(
            client, tags, args.with_phone, not args.no_bio,
            args.include_chats, args.limit,
        )

    merged, added, updated = merge_users(existing, contacts)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"users": merged}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Готово: новых {added}, обновлено полей {updated}, всего в базе {len(merged)} -> {out}")
    await client.disconnect()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api-id", type=int, default=None, help="api_id с my.telegram.org")
    ap.add_argument("--api-hash", default=None, help="api_hash с my.telegram.org")
    ap.add_argument("--phone", default=None, help="номер в формате +79991234567 (спросит при входе)")
    ap.add_argument("--code", default=None, help="код из Telegram (или переменная TG_CODE)")
    ap.add_argument("--password", default=None, help="пароль 2FA (или переменная TG_PASSWORD)")
    ap.add_argument("--proxy-host", default=None, help="MTProxy/прокси хост (напр. cdns.vpnza300.com)")
    ap.add_argument("--proxy-port", default=None, help="порт прокси (по умолчанию 443)")
    ap.add_argument("--proxy-secret", default=None, help="секрет прокси (ee... для fake-TLS)")
    ap.add_argument("--session", default="tools/tg.session", help="файл сессии")
    ap.add_argument("--output", default="data/users.json", help="куда писать базу")
    ap.add_argument("--overwrite", action="store_true",
                    help="перезаписать базу целиком вместо добавления к существующим записям")
    ap.add_argument("--include-chats", action="store_true",
                    help="также брать людей из личных диалогов (не только сохранённые контакты)")
    ap.add_argument("--with-phone", action="store_true",
                    help="включать телефоны в базу (внимание: сайт публичный)")
    ap.add_argument("--no-bio", action="store_true", help="не тянуть био (быстрее, меньше запросов)")
    ap.add_argument("--tag", action="append", default=[],
                    help="добавить тег (можно несколько раз), по умолчанию 'контакт'")
    ap.add_argument("--limit", type=int, default=0, help="обработать только первые N (для проверки)")
    ap.add_argument("--search", nargs="+", default=None,
                    help="режим поиска: искать и сохранять по юзернейму/@/id/номеру")
    args = ap.parse_args()

    asyncio.run(run(args))


if __name__ == "__main__":
    main()