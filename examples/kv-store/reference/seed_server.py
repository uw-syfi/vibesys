"""Minimal Python RESP server — single-threaded asyncio, Python dict.

This is the seed implementation that VibeServe's agent will iterate on.
Speaks a subset of Redis RESP2: string ops (GET, SET, DEL) and hash ops
(HSET, HMSET, HGETALL) needed by YCSB.
"""

import asyncio
import sys

data: dict[bytes, bytes | dict[bytes, bytes]] = {}


def parse_resp(buf: bytes) -> tuple[list[bytes] | None, int]:
    if not buf or buf[0:1] != b"*":
        return None, 0
    crlf = buf.find(b"\r\n")
    if crlf == -1:
        return None, 0
    try:
        num_args = int(buf[1:crlf])
    except ValueError:
        return None, 0
    pos = crlf + 2
    args = []
    for _ in range(num_args):
        if pos >= len(buf) or buf[pos : pos + 1] != b"$":
            return None, 0
        crlf = buf.find(b"\r\n", pos)
        if crlf == -1:
            return None, 0
        try:
            str_len = int(buf[pos + 1 : crlf])
        except ValueError:
            return None, 0
        pos = crlf + 2
        if pos + str_len + 2 > len(buf):
            return None, 0
        args.append(buf[pos : pos + str_len])
        pos += str_len + 2
    return args, pos


def _bulk(val: bytes) -> bytes:
    return b"$" + str(len(val)).encode() + b"\r\n" + val + b"\r\n"


def _ok() -> bytes:
    return b"+OK\r\n"


def _err(msg: str) -> bytes:
    return b"-ERR " + msg.encode() + b"\r\n"


def _wrongtype() -> bytes:
    return b"-WRONGTYPE Operation against a key holding the wrong kind of value\r\n"


def _int(n: int) -> bytes:
    return b":" + str(n).encode() + b"\r\n"


_NULL = b"$-1\r\n"


def _hset(key: bytes, args: list[bytes], start: int) -> int:
    h = data.get(key)
    if not isinstance(h, dict):
        h = {}
        data[key] = h
    created = 0
    for i in range(start, len(args), 2):
        if args[i] not in h:
            created += 1
        h[args[i]] = args[i + 1]
    return created


def handle_command(args: list[bytes]) -> bytes:
    if not args:
        return _err("empty command")
    cmd = args[0].upper()

    if cmd == b"SET":
        if len(args) != 3:
            return _err("wrong number of arguments for 'set' command")
        data[args[1]] = args[2]
        return _ok()

    elif cmd == b"GET":
        if len(args) != 2:
            return _err("wrong number of arguments for 'get' command")
        val = data.get(args[1])
        if isinstance(val, dict):
            return _wrongtype()
        if val is None:
            return _NULL
        return _bulk(val)

    elif cmd == b"DEL":
        if len(args) < 2:
            return _err("wrong number of arguments for 'del' command")
        count = sum(1 for k in args[1:] if data.pop(k, None) is not None)
        return _int(count)

    elif cmd == b"HSET" or cmd == b"HMSET":
        if len(args) < 4 or len(args) % 2 != 0:
            return _err(f"wrong number of arguments for '{cmd.decode().lower()}' command")
        existing = data.get(args[1])
        if isinstance(existing, bytes):
            return _wrongtype()
        created = _hset(args[1], args, 2)
        return _ok() if cmd == b"HMSET" else _int(created)

    elif cmd == b"HGETALL":
        if len(args) != 2:
            return _err("wrong number of arguments for 'hgetall' command")
        h = data.get(args[1])
        if isinstance(h, bytes):
            return _wrongtype()
        if h is None:
            return b"*0\r\n"
        parts = []
        for k, v in h.items():
            parts.append(_bulk(k))
            parts.append(_bulk(v))
        return b"*" + str(len(parts)).encode() + b"\r\n" + b"".join(parts)

    elif cmd == b"DBSIZE":
        if len(args) != 1:
            return _err("wrong number of arguments for 'dbsize' command")
        return _int(len(data))

    elif cmd == b"FLUSHDB" or cmd == b"FLUSHALL":
        if len(args) != 1:
            return _err(f"wrong number of arguments for '{cmd.decode().lower()}' command")
        data.clear()
        return _ok()

    elif cmd == b"PING":
        if len(args) != 1:
            return _err("wrong number of arguments for 'ping' command")
        return b"+PONG\r\n"

    elif cmd == b"COMMAND" or cmd == b"HELLO":
        return b"*0\r\n"

    elif cmd == b"CLIENT":
        return _ok()

    else:
        return _err(f"unknown command '{cmd.decode()}'")


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    buf = b""
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            buf += chunk
            while buf:
                args, consumed = parse_resp(buf)
                if args is None:
                    break
                buf = buf[consumed:]
                writer.write(handle_command(args))
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        writer.close()


async def main(host: str = "0.0.0.0", port: int = 6380):
    server = await asyncio.start_server(handle_client, host, port)
    print(f"Seed KV server listening on {host}:{port}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 6380
    asyncio.run(main(port=port))
