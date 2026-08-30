"""Telethon Connection subclass that wraps the obfuscated2 transport in fake-TLS."""

import asyncio
import logging
import socket

from telethon.network.connection.tcpmtproxy import (
    ConnectionTcpMTProxyRandomizedIntermediate,
)

from .hello import MTProxyFakeTLSClientCodec
from .tls_io import FakeTLSStreamReader, FakeTLSStreamWriter

log = logging.getLogger(__name__)


class ConnectionTcpMTProxyFakeTLS(ConnectionTcpMTProxyRandomizedIntermediate):
    """ee-secret MTProxy connection: TLS 1.3 disguise around obfuscated2.

    Expects ``proxy = (host, port, secret)`` where ``secret`` is the hex string
    *without* the ``ee`` marker — i.e. ``<16-byte-key-hex><domain-hex>``. Or
    base64url without the leading ``7``.
    """

    def __init__(self, ip, port, dc_id, *, loggers, proxy=None, local_addr=None):
        self._fake_tls = MTProxyFakeTLSClientCodec(proxy[2])

        host = proxy[0]
        if len(host) > 60:
            # Hostnames over ~60 chars are almost certainly intended as IPs that got
            # accidentally resolved upstream — push them through gethostbyname so
            # asyncio doesn't choke on the length cap further down.
            host = socket.gethostbyname(proxy[0])

        # Hand the parent class the 16-byte key hex; that's what
        # ConnectionTcpMTProxyRandomizedIntermediate's obfuscated2 layer expects.
        proxy = (host, proxy[1], self._fake_tls.secret.hex())
        super().__init__(ip, port, dc_id, loggers=loggers, proxy=proxy,
                         local_addr=local_addr)

    async def _connect(self, timeout=None, ssl=None):
        local_addr = self._normalize_local_addr()

        if not self._proxy:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host=self._ip, port=self._port, ssl=ssl, local_addr=local_addr,
                ),
                timeout=timeout,
            )
        else:
            sock = await self._proxy_connect(timeout=timeout, local_addr=local_addr)
            if ssl:
                sock = self._wrap_socket_ssl(sock)
            self._reader, self._writer = await asyncio.open_connection(sock=sock)

        await self._fake_tls_handshake()
        self._codec = self.packet_codec(self)
        self._init_conn()
        await self._writer.drain()

    def _normalize_local_addr(self):
        if self._local_addr is None:
            return None
        if isinstance(self._local_addr, tuple) and len(self._local_addr) == 2:
            return self._local_addr
        if isinstance(self._local_addr, str):
            return (self._local_addr, 0)
        raise ValueError(f"Unknown local address format: {self._local_addr!r}")

    async def _fake_tls_handshake(self):
        log.debug("Sending fake-TLS ClientHello")
        self._writer.write(self._fake_tls.build_client_hello())
        await self._writer.drain()

        self._writer = FakeTLSStreamWriter(self._writer)
        self._reader = FakeTLSStreamReader(self._reader)

        server_hello = await self._reader.read_server_hello()
        if not self._fake_tls.verify_server_hello(server_hello):
            raise ConnectionError("fake-TLS ServerHello verification failed")
        log.debug("fake-TLS handshake completed")
