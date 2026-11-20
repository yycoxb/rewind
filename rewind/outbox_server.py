"""A tiny localhost-only external service for idempotent effect PoCs.

The server stores effects under an idempotency key and persists its state with
fsync before acknowledging a request.  It is deliberately local: no external
network access is permitted by this PoC.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class EffectStore:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {
                "effects": {},
                "post_count": 0,
                "dedup_count": 0,
                "get_count": 0,
                "next_seq": 1,
            }
            self._save()

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + f".tmp-{os.getpid()}")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with tmp.open("rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        _fsync_dir(self.path.parent)

    def apply(self, idem_key: str, payload) -> dict:
        with self._lock:
            self.data["post_count"] += 1
            if idem_key in self.data["effects"]:
                self.data["dedup_count"] += 1
                record = dict(self.data["effects"][idem_key])
                record["dedup"] = True
                mismatch = record.get("payload") != payload
                record["payload_mismatch"] = mismatch
                self._save()
                return record
            seq = self.data["next_seq"]
            self.data["next_seq"] = seq + 1
            record = {
                "idem_key": idem_key,
                "payload": payload,
                "server_seq": seq,
                "status": "applied",
                "applied_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            self.data["effects"][idem_key] = record
            # Durable before ack: if the client dies now, the effect survives.
            self._save()
            return {**record, "dedup": False}

    def lookup(self, idem_key: str) -> dict | None:
        with self._lock:
            self.data["get_count"] += 1
            self._save()
            record = self.data["effects"].get(idem_key)
            return dict(record) if record else None

    def stats(self) -> dict:
        with self._lock:
            return {
                "post_count": self.data["post_count"],
                "dedup_count": self.data["dedup_count"],
                "get_count": self.data["get_count"],
                "stored_effects": len(self.data["effects"]),
            }


class Handler(BaseHTTPRequestHandler):
    server_version = "RewindOutbox/1.0"

    @property
    def store(self) -> EffectStore:
        return self.server.store  # type: ignore[attr-defined]

    def _send(self, code: int, body: dict) -> None:
        encoded = (json.dumps(body, sort_keys=True) + "\n").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/effect":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
            idem_key = body["idem_key"]
            payload = body.get("payload")
            if not isinstance(idem_key, str) or not idem_key:
                raise ValueError("idem_key must be a non-empty string")
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})
            return
        record = self.store.apply(idem_key, payload)
        self._send(200, {"ok": True, **record})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send(200, {"ok": True})
            return
        if parsed.path == "/api/stats":
            self._send(200, self.store.stats())
            return
        prefix = "/api/effect/"
        if parsed.path.startswith(prefix):
            idem_key = unquote(parsed.path[len(prefix) :])
            record = self.store.lookup(idem_key)
            if record is None:
                self._send(200, {"found": False, "idem_key": idem_key})
            else:
                self._send(200, {"found": True, "record": record})
            return
        self._send(404, {"error": "not found"})

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return


def serve(store_path: str, port_file: str, host: str = "127.0.0.1", port: int = 0) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    server.store = EffectStore(store_path)  # type: ignore[attr-defined]
    actual_port = server.server_address[1]
    target = Path(port_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{host}:{actual_port}\n", encoding="utf-8")
    with target.open("rb") as fh:
        os.fsync(fh.fileno())
    _fsync_dir(target.parent)
    print(f"OUTBOX_LISTENING {host}:{actual_port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.05)
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="localhost idempotent effect server")
    parser.add_argument("--store", required=True)
    parser.add_argument("--port-file", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        parser.error("only localhost binding is permitted")
    serve(args.store, args.port_file, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
