"""Class-C outbox: intent -> HTTP request -> durable server ack -> result.

If the client dies after the request was sent but before the result record is
fsynced, recovery must not resend.  It enters IN_DOUBT and asks the external
service what happened using the idempotency key.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import restore
from .journal import EFFECT, INTENT, RESULT, Journal

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class OutboxError(RuntimeError):
    pass


def validate_local_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in LOCAL_HOSTS:
        raise OutboxError(f"outbox URL must be localhost HTTP, got {url!r}")
    return url.rstrip("/")


def _request(
    method: str,
    url: str,
    payload: dict | None,
    *,
    timeout: float = 10.0,
) -> dict:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise OutboxError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise OutboxError(f"cannot reach {url}: {exc}") from exc


class OutboxClient:
    def __init__(self, journal: Journal, base_url: str):
        self.journal = journal
        self.base_url = validate_local_url(base_url)

    def execute(
        self,
        idem_key: str,
        payload,
        *,
        crash_after_ack: bool = False,
    ) -> dict:
        # The intent is durable before the request leaves this process.
        self.journal.append(
            INTENT,
            op="external_effect",
            effect_class="C",
            idem_key=idem_key,
            payload=payload,
            endpoint=f"{self.base_url}/api/effect",
            note="irreversible external effect; intent fsynced before POST",
        )
        ack = _request(
            "POST",
            f"{self.base_url}/api/effect",
            {"idem_key": idem_key, "payload": payload},
        )
        if not ack.get("ok"):
            raise OutboxError(f"server rejected effect: {ack}")
        if crash_after_ack:
            # Real crash at the dangerous window: the server has durably
            # applied the effect, but this client has not recorded the ack.
            os.kill(os.getpid(), signal.SIGKILL)
        self.journal.append(
            RESULT,
            op="external_effect",
            effect_class="C",
            idem_key=idem_key,
            kind="ack",
            status="applied",
            found=True,
            server_seq=ack.get("server_seq"),
            dedup=ack.get("dedup", False),
            ack=ack,
        )
        self.journal.append(
            EFFECT,
            op="external_effect",
            effect_class="C",
            idem_key=idem_key,
            phase="acknowledged",
            server_seq=ack.get("server_seq"),
        )
        return ack

    def lookup(self, idem_key: str) -> dict:
        quoted = urllib.parse.quote(idem_key, safe="")
        return _request(
            "GET", f"{self.base_url}/api/effect/{quoted}", None
        )

    def stats(self) -> dict:
        return _request("GET", f"{self.base_url}/api/stats", None)

    def reconcile(self, idem_key: str) -> dict:
        """Read-only reconciliation.  Never POSTs and never retries."""

        response = self.lookup(idem_key)
        found = bool(response.get("found"))
        record = response.get("record") or {}
        self.journal.append(
            RESULT,
            op="external_effect",
            effect_class="C",
            idem_key=idem_key,
            kind="reconcile",
            status="reconciled" if found else "not_found",
            found=found,
            server_seq=record.get("server_seq"),
            server_record=record,
            note="read-only lookup by idempotency key; no retry",
        )
        self.journal.append(
            EFFECT,
            op="external_effect",
            effect_class="C",
            idem_key=idem_key,
            phase="reconciled",
            found=found,
            server_seq=record.get("server_seq"),
        )
        return response


@dataclass
class Reconciliation:
    status: str
    items: list[dict]
    unresolved: list[dict]

    @property
    def reconciled(self) -> bool:
        return self.status == restore.IN_DOUBT and not self.unresolved


def recover_external_effects(
    journal: Journal,
    base_url: str,
) -> Reconciliation:
    """Enter IN_DOUBT for every unresolved class-C intent, then query."""

    client = OutboxClient(journal, base_url)
    pending = journal.unresolved_external_effects()
    if not pending:
        return Reconciliation(status="NONE", items=[], unresolved=[])
    items: list[dict] = []
    unresolved: list[dict] = []
    for intent in pending:
        key = intent["idem_key"]
        try:
            response = client.reconcile(key)
        except OutboxError as exc:
            unresolved.append({"idem_key": key, "error": str(exc)})
            continue
        items.append(
            {
                "idem_key": key,
                "found": bool(response.get("found")),
                "server_seq": (response.get("record") or {}).get("server_seq"),
            }
        )
    return Reconciliation(
        status=restore.IN_DOUBT,
        items=items,
        unresolved=unresolved,
    )


@dataclass
class ServerHandle:
    process: subprocess.Popen
    url: str
    store: Path
    port_file: Path

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.process.stdout is not None:
            self.process.stdout.close()


def start_local_server(
    workdir: str | os.PathLike[str],
    *,
    store: str | os.PathLike[str],
    port_file: str | os.PathLike[str],
    timeout: float = 15.0,
    echo: bool = False,
) -> ServerHandle:
    store_path = Path(store)
    port_path = Path(port_file)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    port_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        port_path.unlink()
    except FileNotFoundError:
        pass
    env = os.environ.copy()
    package_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = package_root + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "rewind.outbox_server",
        "--store",
        os.fspath(store_path),
        "--port-file",
        os.fspath(port_path),
    ]
    process = subprocess.Popen(
        cmd,
        cwd=os.fspath(workdir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_path.exists():
            text = port_path.read_text(encoding="utf-8").strip()
            if text:
                host, port = text.rsplit(":", 1)
                if host == "::1":
                    host = "[::1]"
                url = f"http://{host}:{port}"
                # health check
                for _ in range(20):
                    try:
                        _request("GET", f"{url}/health", None)
                        return ServerHandle(process, url, store_path, port_path)
                    except OutboxError:
                        time.sleep(0.05)
        if process.poll() is not None:
            out = process.stdout.read() if process.stdout else ""
            raise OutboxError(f"outbox server exited early: {out}")
        time.sleep(0.02)
    process.terminate()
    raise OutboxError("outbox server did not become ready")
