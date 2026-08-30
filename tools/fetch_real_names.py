#!/usr/bin/env python3
"""Получает НАСТОЯЩИЕ имена пользователей через второй (чистый) аккаунт.

Первый аккаунт (сессия 1) знает всех людей (ID + access_hash).
Второй аккаунт (сессия 2) не имеет твоих телефонных подписей, поэтому сервер
возвращает имена, которые люди сами себе указали.

Пример:
  python tools/fetch_real_names.py --api-id 123 --api-hash xxx \
      --session1 tools/tg.session --phone2 +573147718514 --code 12345
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.network import (
    ConnectionTcpMTProxyIntermediate,
    ConnectionTcpMTProxyRandomizedIntermediate,
)
from telethon.tl.types import InputPeerUser
from telethon.tl.functions.users import GetUsersRequest


def clean(value):
    return " ".join(str(value or "").split()).strip()


def build_client(session, api_id, api_hash, proxy):
    kwargs = {}
    if proxy:
        host, port, secret = proxy
        port = int(port)
        if secret.lower().startswith("ee"):
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import telethon_faketls
            kwargs.update(connection=telethon_faketls.ConnectionTcpMTProxyFakeTLS,
                          proxy=(host, port, secret[2:]))
        elif secret.lower().startswith("dd"):
            kwargs.update(connection=ConnectionTcpMTProxyRandomizedIntermediate,
                          proxy=(host, port, secret[2:]))
        else:
            kwargs.update(connection=ConnectionTcpMTProxyIntermediate,
                          proxy=(host, port, secret))
    Path(session).parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(session, int(api_id), api_hash, **kwargs)


async def login(client, args, phone_env_key):
    await client.connect()
    if await client.is_user_authorized():
        return
    phone = getattr(args, "phone2", None) or os.environ.get(phone_env_key)
    if not phone:
        raise SystemExit("Укажи --phone2 (или переменную окружения).")
    code = args.code or os.environ.get("TG_CODE", "")
    password = args.password or os.environ.get("TG_PASSWORD", "")
    try:
        await client.send_code_request(phone)
        _code = code or input("Код из Telegram: ")
        try:
            await client.sign_in(phone, _code)
        except SessionPasswordNeededError:
            await client.sign_in(password=password or input("Пароль 2FA: "))
    except EOFError:
        raise SystemExit("Код отправлен на второй номер. Повтори запуск с --code <код> "
                         "(и --password, если включена 2FA).")
    except SessionPasswordNeededError:
        await client.sign_in(password=password or input("Пароль 2FA: "))


async def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api-id", type=int, default=None)
    ap.add_argument("--api-hash", default=None)
    ap.add_argument("--session1", default="tools/tg.session",
                    help="сессия первого аккаунта (у которого есть контакты)")
    ap.add_argument("--session2", default="tools/second.session",
                    help="сессия второго (чистого) аккаунта — туда пишем")
    ap.add_argument("--phone2", default=None, help="номер второго аккаунта")
    ap.add_argument("--code", default=None, help="код входа второго аккаунта")
    ap.add_argument("--password", default=None, help="пароль 2FA второго аккаунта")
    ap.add_argument("--output", default="data/users.json")
    ap.add_argument("--proxy-host", default=None)
    ap.add_argument("--proxy-port", default=None)
    ap.add_argument("--proxy-secret", default=None)
    args = ap.parse_args()

    if not (args.api_id and args.api_hash):
        raise SystemExit("Укажи --api-id и --api-hash.")
    if not (args.proxy_host and args.proxy_secret):
        raise SystemExit("Нужен --proxy-host/--proxy-port/--proxy-secret (Telegram заблокирован).")

    proxy = (args.proxy_host, args.proxy_port or 7443, args.proxy_secret)

    db_path = Path(args.output)
    db = json.loads(db_path.read_text(encoding="utf-8"))
    users = db["users"]

    client1 = build_client(args.session1, args.api_id, args.api_hash, proxy)
    await login(client1, args, "TG_PHONE")
    me1 = await client1.get_me()
    print(f"Аккаунт 1: {me1.first_name} (id {me1.id})")

    phone_by_id = {}
    try:
        from telethon.tl.functions.contacts import GetContactsRequest
        me1 = await client1.get_me()
        contacts = await client1(GetContactsRequest(hash=0))
        for u in contacts.users:
            phone = (u.phone or "")
            if phone or (getattr(u, "id", None) is not None and phone):
                phone_by_id[u.id] = phone
    except Exception as exc:
        print(f"  не удалось получить телефоны контактов: {type(exc).__name__} {str(exc)[:80]}")

    candidates = []
    for u in users:
        uid = u.get("id")
        if uid is None:
            continue
        candidates.append((uid,
                           (u.get("username") or "").lstrip("@"),
                           phone_by_id.get(uid) or ""))
    print(f"Собрано кандидатов: {len(candidates)} (телефонов: {sum(1 for _, _, p in candidates if p)})")
    await client1.disconnect()

    client2 = build_client(args.session2, args.api_id, args.api_hash, proxy)
    await login(client2, args, "TG_PHONE2")
    me2 = await client2.get_me()
    print(f"Аккаунт 2: {me2.first_name} (id {me2.id})")

    from telethon.tl.functions.contacts import DeleteContactsRequest, ImportContactsRequest
    from telethon.tl.types import InputPhoneContact, InputUser

    found = {}
    for uid, uname, phone in candidates:
        info = None
        if uname:
            try:
                u = await client2.get_entity(uname)
                info = {"name": clean(" ".join(filter(None, [u.first_name, u.last_name]))),
                        "username": (u.username or "").lstrip("@")}
            except Exception as exc:
                print(f"  {uname or uid}: юзернейм не помог ({type(exc).__name__} {str(exc)[:70]})")
        if info is None and phone:
            try:
                cid = (uid * 2 + 7) % (2 ** 31)
                imp = await client2(ImportContactsRequest(
                    [InputPhoneContact(client_id=cid, phone="".join(ch for ch in phone if ch.isdigit()),
                                       first_name=".", last_name=".")]))
                u = imp.users[0] if imp.users else None
                if imp.users:
                    await client2(DeleteContactsRequest([InputUser(u.id, u.access_hash)]))
                info = u and {"name": clean(" ".join(filter(None, [u.first_name, u.last_name]))),
                              "username": (u.username or "").lstrip("@")}
            except Exception as exc:
                print(f"  id {uid}: телефон не помог ({type(exc).__name__} {str(exc)[:70]})")
        found[uid] = info
    await client2.disconnect()

    changed = 0
    for u in users:
        uid = u.get("id")
        info = found.get(uid)
        if info is None:
            continue
        real = info["name"]
        old = u.get("name")
        if real and real != (old or ""):
            if old and old not in ("None", "") and not u.get("saved_as"):
                u["saved_as"] = old
            u["name"] = real
            changed += 1
        if info["username"] and info["username"] != (u.get("username") or ""):
            u["username"] = info["username"]
        if u.get("saved_as") in (None, "", "None") or u.get("name") == "" and u.get("saved_as") == "None":
            u.pop("saved_as", None)

    db_path.write_text(json.dumps({"users": users}, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
    print(f"Готово: обновлено имён: {changed}, всего записей: {len(users)}")

    await client1.disconnect()
    await client2.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit as e:
        if e.code:
            print(e.code)
        sys.exit(1 if e.code else 0)