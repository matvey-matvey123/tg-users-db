"""Reader/writer that wraps an asyncio stream in TLS 1.3 Application-Data records."""

import logging

log = logging.getLogger(__name__)


class _LayeredReader:
    __slots__ = ("upstream",)

    def __init__(self, upstream):
        self.upstream = upstream

    async def read(self, n):
        return await self.upstream.read(n)

    async def readexactly(self, n):
        return await self.upstream.readexactly(n)


class _LayeredWriter:
    __slots__ = ("upstream",)

    def __init__(self, upstream):
        self.upstream = upstream

    def write(self, data):
        return self.upstream.write(data)

    def write_eof(self):
        return self.upstream.write_eof()

    async def drain(self):
        return await self.upstream.drain()

    async def wait_closed(self):
        return await self.upstream.wait_closed()

    def close(self):
        return self.upstream.close()

    def abort(self):
        return self.upstream.transport.abort()

    def get_extra_info(self, name):
        return self.upstream.get_extra_info(name)

    @property
    def transport(self):
        return self.upstream.transport


class FakeTLSStreamReader(_LayeredReader):
    """Strips TLS record framing from incoming bytes."""

    __slots__ = ("buf",)

    def __init__(self, upstream):
        super().__init__(upstream)
        self.buf = bytearray()

    async def _read_record(self):
        """Read one full TLS record and return (record_type, payload)."""
        rec_type = await self.upstream.readexactly(1)
        if not rec_type:
            return None, b""
        version = await self.upstream.readexactly(2)
        if version != b"\x03\x03":
            log.error("Unexpected TLS version %r", version)
            return None, b""
        data_len = int.from_bytes(await self.upstream.readexactly(2), "big")
        data = await self.upstream.readexactly(data_len)
        return rec_type, data

    async def read(self, n, ignore_buf=False):
        if self.buf and not ignore_buf:
            data = bytes(self.buf)
            self.buf = bytearray()
            return data

        while True:
            rec_type, data = await self._read_record()
            if rec_type is None:
                return b""
            if rec_type == b"\x17":  # Application Data
                return data
            if rec_type in (b"\x14", b"\x15", b"\x16"):
                # ChangeCipherSpec / Alert / Handshake noise — skip
                continue
            log.error("Unexpected TLS record type %r", rec_type)
            return b""

    async def readexactly(self, n):
        while len(self.buf) < n:
            chunk = await self.read(1, ignore_buf=True)
            if not chunk:
                return b""
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return bytes(data)

    async def read_server_hello(self) -> bytes:
        """Read a TDesktop-style variable-length ServerHello.

        Layout (as sent by Telegram Desktop's TLS proxy — see
        mtproto_tls_socket.cpp):
            Part1: 16 03 03 + 2-byte BE length of Part2
            Part2: that many bytes (the TLS ServerHello handshake message)
            Part3: 14 03 03 00 01 01 17 03 03 + 2-byte BE length of Part4
            Part4: that many bytes (application data payload)
        """
        head = await self.upstream.readexactly(5)
        if not head:
            return b""
        if head[:3] != b"\x16\x03\x03":
            raise ConnectionError("fake-TLS: bad ServerHello record header")
        part2_size = int.from_bytes(head[3:5], "big")
        part2 = await self.upstream.readexactly(part2_size)
        ccs_and_header = await self.upstream.readexactly(9)
        if ccs_and_header[:9] != b"\x14\x03\x03\x00\x01\x01\x17\x03\x03":
            raise ConnectionError("fake-TLS: bad ServerHello CCS/appdata header")
        part4_size = int.from_bytes(await self.upstream.readexactly(2), "big")
        part4 = await self.upstream.readexactly(part4_size)
        return head + part2 + ccs_and_header + part4_size.to_bytes(2, "big") + part4


class FakeTLSStreamWriter(_LayeredWriter):
    """Wraps outgoing bytes in TLS 1.3 Application-Data records."""

    __slots__ = ("_sent_ccs",)

    def __init__(self, upstream):
        super().__init__(upstream)
        self._sent_ccs = False

    MAX_CHUNK = 16384 + 24

    def write(self, data):
        if not self._sent_ccs:
            # Telegram Desktop sends a ChangeCipherSpec record before the
            # first Application Data record from the client.
            self.upstream.write(b"\x14\x03\x03\x00\x01\x01")
            self._sent_ccs = True
        for start in range(0, len(data), self.MAX_CHUNK):
            end = min(start + self.MAX_CHUNK, len(data))
            self.upstream.write(b"\x17\x03\x03" + (end - start).to_bytes(2, "big"))
            self.upstream.write(data[start:end])
        return len(data)