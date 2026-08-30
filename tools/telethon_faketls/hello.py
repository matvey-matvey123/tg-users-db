"""Build and verify the fake TLS 1.3 ClientHello/ServerHello pair.

Ported from Telegram-iOS MtProtoKit/Sources/MTTcpConnection.m
(MTCreateSafariClientHello / executeGenerationCode), which mimics a Safari
TLS 1.3 ClientHello so JA3/JA4 fingerprints look like a real browser.

The 32-byte "random" field is in fact
    HMAC_SHA256(secret, ClientHello_with_random_zeroed)
with the last 4 bytes XORed with the current unix timestamp (LE) — that
binds the handshake to the proxy secret and gives the server replay
protection.

The iOS DSL opcodes:
  S "..."  append literal bytes (decoded from \\xNN escapes)
  Z N      append N zero bytes (digest placeholder)
  R N      append N random bytes
  D        append the SNI domain bytes
  G N      append the N-th GREASE byte twice (yields a 0x?A 0x?A pair)
  K        append a 32-byte fake X25519 public key
  M        append a 1184-byte fake ML-KEM-768 public key
  [   ]    open / close a 16-bit length-prefixed block
  < ( ) >  random choice between alternatives
"""

import base64
import hashlib
import hmac
import logging
import re
import secrets
import time

log = logging.getLogger(__name__)

DIGEST_POS = 11
DIGEST_LEN = 32

# The DSL string is taken verbatim from MTTcpConnection.m's executeGenerationCode.
# Whitespace is irrelevant to the parser.
_HELLO_DSL = r"""
S "\x16\x03\x01"
[
  S "\x01\x00"
  [
    S "\x03\x03"
    Z 32
    S "\x20"
    R 32
    <
      ( S "\x00\x1c" G 0 S "\x13\x02\x13\x01\x13\x03\xc0\x2c\xc0\x30\xc0\x2b\xcc\xa9\xc0\x2f\xcc\xa8\xc0\x0a\xc0\x09\xc0\x14\xc0\x13" )
      ( S "\x00\x2a" G 0 S "\x13\x02\x13\x03\x13\x01\xc0\x2c\xc0\x2b\xcc\xa9\xc0\x30\xc0\x2f\xcc\xa8\xc0\x0a\xc0\x09\xc0\x14\xc0\x13\x00\x9d\x00\x9c\x00\x35\x00\x2f\xc0\x08\xc0\x12\x00\x0a" )
    >
    S "\x01\x00"
    [
      G 2
      S "\x00\x00\x00\x00"
      [
        [
          S "\x00"
          [
            D
          ]
        ]
      ]
      S "\x00\x17\x00\x00\xff\x01\x00\x01\x00\x00\x0a\x00\x0e\x00\x0c"
      G 4
      S "\x11\xec\x00\x1d\x00\x17\x00\x18\x00\x19\x00\x0b\x00\x02\x01\x00"
      <
        ( S "\x00\x10\x00\x0b\x00\x09\x08\x68\x74\x74\x70\x2f\x31\x2e\x31" )
        ( S "\x00\x10\x00\x0e\x00\x0c\x02\x68\x32\x08\x68\x74\x74\x70\x2f\x31\x2e\x31" )
      >
      S "\x00\x05\x00\x05\x01\x00\x00\x00\x00\x00\x0d\x00\x16\x00\x14\x04\x03\x08\x04\x04\x01\x05\x03\x08\x05\x08\x05\x05\x01\x08\x06\x06\x01\x02\x01\x00\x12\x00\x00\x00\x33\x04\xef\x04\xed"
      G 4
      S "\x00\x01\x00\x11\xec\x04\xc0"
      M
      K
      S "\x00\x1d\x00\x20"
      K
      S "\x00\x2d\x00\x02\x01\x01\x00\x2b\x00\x07\x06"
      G 6
      S "\x03\x04\x03\x03\x00\x1b\x00\x03\x02\x00\x01"
      G 3
      S "\x00\x01\x00"
    ]
  ]
]
"""

# Pre-tokenize: turn S "..." into an explicit literal so we don't reparse the
# escape every build. Each token is (opcode, payload) where payload depends on
# opcode: bytes for "S", int for "Z"/"R"/"G", None for "D"/"K"/"M"/"[" / "]"
# / "<" / "(" / ")" / ">".
_S_RE = re.compile(r'S\s+"((?:\\x[0-9a-fA-F]{2}|[^"\\])*)"')
_NUM_RE = re.compile(r"\s*(\d+)")


def _decode_escaped(s):
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s).encode("latin-1")


def _tokenize(dsl):
    tokens = []
    i = 0
    while i < len(dsl):
        c = dsl[i]
        if c.isspace():
            i += 1
            continue
        if c == "S":
            m = _S_RE.match(dsl, i)
            if not m:
                raise ValueError(f"bad S token at {i}")
            tokens.append(("S", _decode_escaped(m.group(1))))
            i = m.end()
        elif c in ("Z", "R", "G"):
            m = _NUM_RE.match(dsl, i + 1)
            if not m:
                raise ValueError(f"bad {c} token at {i}")
            tokens.append((c, int(m.group(1))))
            i = m.end()
        elif c in "DKM[]<()>":
            tokens.append((c, None))
            i += 1
        else:
            raise ValueError(f"unknown DSL char {c!r} at {i}")
    return tokens


_HELLO_TOKENS = _tokenize(_HELLO_DSL)


def _gen_grease():
    # 8 bytes shaped as 0x?a, with adjacent slots forced to differ.
    g = bytearray(secrets.token_bytes(8))
    for i in range(8):
        g[i] = (g[i] & 0xF0) | 0x0A
    for i in range(0, 8, 2):
        if g[i] == g[i + 1]:
            g[i + 1] ^= 0x10
    return bytes(g)


def _gen_x25519_fake():
    """Random 32-byte value that's a quadratic residue mod (2^255 - 19).

    The proxy never does real X25519, so this only needs to look like a key.
    """
    p = 2 ** 255 - 19
    n = int.from_bytes(secrets.token_bytes(32), "big") % p
    return ((n * n) % p).to_bytes(32, "little")


def _gen_ml_kem_fake():
    """1184 bytes shaped like an ML-KEM-768 public key.

    Mirrors generate_ml_kem_public_key in MTTcpConnection.m: 384 packed 12-bit
    integer pairs in [0, 3329) followed by 32 random bytes. The proxy doesn't
    perform key encapsulation, so coefficients can be random.
    """
    out = bytearray(1184)
    for i in range(384):
        a = secrets.randbelow(3329)
        b = secrets.randbelow(3329)
        out[i * 3] = a & 0xFF
        out[i * 3 + 1] = ((a >> 8) & 0x0F) | ((b & 0x0F) << 4)
        out[i * 3 + 2] = (b >> 4) & 0xFF
    out[1152:1184] = secrets.token_bytes(32)
    return bytes(out)


def _execute(tokens, domain_bytes, grease):
    """Execute the DSL token stream into a bytes buffer."""
    out = bytearray()
    length_stack = []

    def run(start):
        i = start
        while i < len(tokens):
            op, arg = tokens[i]
            i += 1

            if op == "S":
                out.extend(arg)
            elif op == "Z":
                out.extend(b"\x00" * arg)
            elif op == "R":
                out.extend(secrets.token_bytes(arg))
            elif op == "D":
                out.extend(domain_bytes)
            elif op == "G":
                v = grease[arg]
                out.append(v)
                out.append(v)
            elif op == "K":
                out.extend(_gen_x25519_fake())
            elif op == "M":
                out.extend(_gen_ml_kem_fake())
            elif op == "[":
                length_stack.append(len(out))
                out.extend(b"\x00\x00")
            elif op == "]":
                pos = length_stack.pop()
                blen = len(out) - pos - 2
                if blen > 0xFFFF:
                    raise ValueError("DSL block exceeded 16-bit length")
                out[pos] = (blen >> 8) & 0xFF
                out[pos + 1] = blen & 0xFF
            elif op == "<":
                # Collect alternative slices.
                alts = []
                while i < len(tokens) and tokens[i][0] != ">":
                    if tokens[i][0] != "(":
                        raise ValueError("expected ( inside choice")
                    i += 1
                    alts.append(i)
                    # Find matching ")"
                    depth = 0
                    while i < len(tokens):
                        t = tokens[i][0]
                        if t == "(":
                            depth += 1
                        elif t == ")":
                            if depth == 0:
                                break
                            depth -= 1
                        i += 1
                    if i >= len(tokens):
                        raise ValueError("unterminated alternative")
                    i += 1  # skip ")"
                if i >= len(tokens) or tokens[i][0] != ">":
                    raise ValueError("expected > after choice")
                i += 1  # skip ">"
                # Pick one alternative and execute it inline.
                pick = secrets.choice(alts)
                # Find its end so we can run that slice in isolation.
                j = pick
                depth = 0
                while j < len(tokens):
                    t = tokens[j][0]
                    if t == "(":
                        depth += 1
                    elif t == ")":
                        if depth == 0:
                            break
                        depth -= 1
                    j += 1
                run_slice(pick, j)
            else:
                raise ValueError(f"unhandled op {op}")
        return i

    def run_slice(lo, hi):
        # Save and rebind tokens window, then restore.
        nonlocal tokens
        original = tokens
        tokens = original[lo:hi]
        try:
            run(0)
        finally:
            tokens = original

    run(0)
    if length_stack:
        raise ValueError("unclosed length block in DSL")
    return bytes(out)


def _decode_b64(s):
    s = re.sub(r"[^a-zA-Z0-9+/]+", "", s)
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


class MTProxyFakeTLSClientCodec:
    """Build/verify ee-secret fake-TLS handshakes with a Safari fingerprint."""

    def __init__(self, secret: str):
        # Accept either hex (without the ee marker) or base64url (without leading 7).
        try:
            full = bytes.fromhex(f"ee{secret}")
        except ValueError:
            full = _decode_b64(f"7{secret}")

        if len(full) < 18:
            raise ValueError(f"secret too short: {len(full)} bytes")

        self.secret = full[1:17]
        self.domain = full[17:]
        self._last_session_id = b""
        self._last_client_random = b""

    def build_client_hello(self) -> bytes:
        grease = _gen_grease()
        hello = bytearray(_execute(_HELLO_TOKENS, self.domain, grease))

        # Extract the just-generated session_id so we can verify the ServerHello echo.
        if len(hello) < DIGEST_POS + DIGEST_LEN + 1:
            raise ValueError("hello too short — DSL produced unexpected layout")
        session_id_len = hello[DIGEST_POS + DIGEST_LEN]
        sid_start = DIGEST_POS + DIGEST_LEN + 1
        self._last_session_id = bytes(hello[sid_start : sid_start + session_id_len])

        # Pad to 513 bytes (matches iOS) — only triggers when ML-KEM is omitted, but kept
        # so the length stays consistent if we ever drop the ML-KEM branch.
        if len(hello) < 513:
            pad_len = 513 - len(hello)
            hello.extend(b"\x00\x15")
            hello.extend(pad_len.to_bytes(2, "big"))
            hello.extend(b"\x00" * pad_len)

        # Compute HMAC over the full hello with digest bytes zeroed, then XOR
        # the trailing 4 bytes with the LE unix timestamp.
        for i in range(DIGEST_LEN):
            hello[DIGEST_POS + i] = 0
        digest = bytearray(hmac.new(self.secret, bytes(hello), hashlib.sha256).digest())
        ts = int(time.time()).to_bytes(4, "little")
        for i in range(4):
            digest[DIGEST_LEN - 4 + i] ^= ts[i]
        hello[DIGEST_POS : DIGEST_POS + DIGEST_LEN] = digest
        self._last_client_random = bytes(digest)
        return bytes(hello)

    def verify_server_hello(self, server_hello: bytes) -> bool:
        try:
            if len(server_hello) < 5 + 9 + 2:
                raise ValueError("invalid size")
            if not server_hello.startswith(b"\x16\x03\x03"):
                raise ValueError("invalid record header")
            # Variable-length ServerHello (Telegram Desktop layout):
            #   Part1: 16 03 03 + 2-byte BE Part2 length
            #   Part2: the ServerHello handshake message (Part2 length bytes)
            #   Part3: 14 03 03 00 01 01 17 03 03 + 2-byte BE Part4 length
            #   Part4: application data payload
            part2_size = int.from_bytes(server_hello[3:5], "big")
            ccs_offset = 5 + part2_size
            if len(server_hello) < ccs_offset + 9 + 2:
                raise ValueError("invalid size")
            if server_hello[ccs_offset : ccs_offset + 9] != (
                b"\x14\x03\x03\x00\x01\x01\x17\x03\x03"
            ):
                raise ValueError("invalid CCS / app-data follow-up")
            # ServerHello.session_id starts at:
            #   record_header(5) + handshake_type(1) + handshake_len(3) +
            #   server_version(2) + server_random(32) + session_id_len(1) = 44
            if server_hello[44 : 44 + 32] != self._last_session_id:
                raise ValueError("session id mismatch")

            sh = bytearray(server_hello)
            server_digest = bytes(sh[11 : 11 + 32])
            sh[11 : 11 + 32] = b"\x00" * 32

            computed = hmac.new(
                self.secret, self._last_client_random + bytes(sh), hashlib.sha256
            ).digest()
            if not hmac.compare_digest(server_digest, computed):
                raise ValueError("server digest mismatch")
        except Exception as ex:
            log.error("ServerHello verify failed: %s", ex)
            return False
        return True
