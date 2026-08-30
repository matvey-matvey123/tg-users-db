"""Fake-TLS MTProxy connection for Telethon.

Implements the TLS 1.3 ClientHello / ServerHello disguise used by ``ee``-prefix
MTProxy secrets, so Telethon can connect through fake-TLS MTProxies (which stock
Telethon does not support natively).

Usage::

    from telethon import TelegramClient
    from telethon_faketls import ConnectionTcpMTProxyFakeTLS

    client = TelegramClient(
        session, api_id, api_hash,
        proxy=(host, port, secret),   # secret = ee-secret hex without the "ee"
        connection=ConnectionTcpMTProxyFakeTLS,
    )

Provenance: the connection layer subclasses Telethon's
``ConnectionTcpMTProxyRandomizedIntermediate``; the handshake in :mod:`hello` is
ported from Telegram-iOS MtProtoKit. See NOTICE for details.

Coupled to Telethon 1.x connection internals — this package pins ``telethon<2``.
"""

from .connection import ConnectionTcpMTProxyFakeTLS

__all__ = ["ConnectionTcpMTProxyFakeTLS"]
__version__ = "0.1.0"
