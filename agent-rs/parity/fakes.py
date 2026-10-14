"""Scripted fake servers for the offline parity harness (PLAN.md 7.4).

Every server answers **by request index per endpoint**: the n-th request to
an endpoint gets the n-th scripted response, and the last response repeats
for every further request. Each agent gets its own instances (own ports),
so the n-th sample of a check sees the same response in both runs no matter
how the two processes are scheduled. The DNS server is the exception: it
must listen on port 53 (neither agent can be pointed at another port), so
one instance serves both agents and keys its scripts by query name, which
carries the agent label.

Standard library plus dnspython (a nyxmon dependency) only.
"""

from __future__ import annotations

import datetime as dt
import json
import socket
import socketserver
import ssl
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import dns.flags
import dns.message
import dns.rcode
import dns.rrset


class Script:
    """Thread-safe per-endpoint response scripts with request counters."""

    def __init__(self) -> None:
        self._scripts: dict[str, list[Any]] = {}
        self._served: dict[str, int] = {}
        self._lock = threading.Lock()

    def set(self, key: str, responses: list[Any]) -> None:
        if not responses:
            raise ValueError(f"empty script for {key}")
        self._scripts[key] = list(responses)

    def next(self, key: str) -> Any | None:
        """The response for the next request to ``key`` (``None``: unknown)."""
        with self._lock:
            responses = self._scripts.get(key)
            if responses is None:
                return None
            index = self._served.get(key, 0)
            self._served[key] = index + 1
            return responses[min(index, len(responses) - 1)]

    def served(self) -> dict[str, int]:
        with self._lock:
            return dict(self._served)


class _Threaded:
    """A daemon thread running ``serve_forever`` of a socketserver."""

    def __init__(self, server: socketserver.BaseServer) -> None:
        self.server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# --------------------------------------------------------------------- HTTP


class _HttpHandler(BaseHTTPRequestHandler):
    server: _HttpServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _answer(self) -> None:
        response = self.server.script.next(self.path.split("?", 1)[0])
        if response is None:
            response = {"status": 404, "body": "unknown endpoint"}
        delay = float(response.get("delay", 0))
        if delay:
            # Waits for the client's timeout; the response may never be read.
            threading.Event().wait(delay)
        body = response.get("body", "")
        if not isinstance(body, (str, bytes)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(int(response.get("status", 200)))
        headers = dict(response.get("headers", {}))
        headers.setdefault("Content-Type", "application/json")
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except OSError:
                pass
        self.close_connection = True

    do_GET = _answer
    do_HEAD = _answer
    do_POST = _answer


class _HttpServer(ThreadingHTTPServer):
    daemon_threads = True
    # A batch connects to many endpoints at once.
    request_queue_size = 128
    script: Script


class HttpFake:
    """Scripted HTTP endpoints, keyed by path."""

    def __init__(self) -> None:
        self.script = Script()
        server = _HttpServer(("127.0.0.1", 0), _HttpHandler)
        server.script = self.script
        self.port = server.server_address[1]
        self._runner = _Threaded(server)

    def close(self) -> None:
        self._runner.close()


# ---------------------------------------------------------------------- TCP


class TcpFake:
    """One listener; per connection one action: ``plain`` (accept and hold
    until the client closes), ``garbage`` (answer the ClientHello with a
    plain-text line and close, a deterministic TLS handshake failure) or
    ``tls:<name>`` (TLS handshake with the named server context, hold, and
    answer the client's close_notify) or ``tls-abrupt:<name>`` (the same,
    but closing the TCP connection without close_notify).

    A reset right after accept is not offered: whether the client sees it
    as a failed connect or a failed handshake depends on scheduling, in
    either implementation."""

    def __init__(self, contexts: dict[str, ssl.SSLContext], actions: list[str]) -> None:
        self.script = Script()
        self.script.set("conn", actions)
        self._contexts = contexts
        self._listener = socket.create_server(("127.0.0.1", 0), backlog=128)
        self.port = self._listener.getsockname()[1]
        self._closed = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self._closed.is_set():
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            action = self.script.next("conn")
            threading.Thread(
                target=self._serve, args=(connection, action), daemon=True
            ).start()

    def _serve(self, connection: socket.socket, action: str) -> None:
        connection.settimeout(10)
        try:
            if action == "garbage":
                connection.recv(4096)
                connection.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return
            kind, _, context = action.partition(":")
            if kind in ("tls", "tls-abrupt"):
                connection = self._contexts[context].wrap_socket(
                    connection, server_side=True
                )
            while connection.recv(4096):
                pass
            if kind == "tls" and isinstance(connection, ssl.SSLSocket):
                # Answer the client's close_notify like a real TLS server.
                connection = connection.unwrap()
        except (OSError, ssl.SSLError):
            pass
        finally:
            connection.close()

    def close(self) -> None:
        self._closed.set()
        self._listener.close()


# --------------------------------------------------------------------- SMTP


class SmtpFake:
    """A plain SMTP server; per connection one action: ``ok``,
    ``rcpt_refused`` (550 on RCPT TO), ``mail_temporary`` (451 on MAIL
    FROM) or ``data_rejected`` (554 after DATA)."""

    def __init__(self, actions: list[str]) -> None:
        self.script = Script()
        self.script.set("conn", actions)
        self.messages: list[bytes] = []
        self._listener = socket.create_server(("127.0.0.1", 0), backlog=128)
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            action = self.script.next("conn")
            threading.Thread(
                target=self._serve, args=(connection, action), daemon=True
            ).start()

    def _serve(self, connection: socket.socket, action: str) -> None:
        connection.settimeout(10)
        reader = connection.makefile("rb")

        def send(line: str) -> None:
            connection.sendall(line.encode() + b"\r\n")

        try:
            send("220 parity.test ESMTP fake")
            while True:
                raw = reader.readline()
                if not raw:
                    return
                command = raw.decode("ascii", "replace").strip()
                verb = command.split(" ", 1)[0].upper()
                if verb in ("EHLO", "HELO"):
                    send("250-parity.test")
                    send("250 8BITMIME")
                elif verb == "MAIL":
                    if action == "mail_temporary":
                        send("451 4.7.1 Greylisted, try again later")
                    else:
                        send("250 2.1.0 Ok")
                elif verb == "RCPT":
                    if action == "rcpt_refused":
                        send("550 5.1.1 No such user")
                    else:
                        send("250 2.1.5 Ok")
                elif verb == "DATA":
                    send("354 End data with <CR><LF>.<CR><LF>")
                    lines = []
                    while True:
                        line = reader.readline()
                        if not line or line == b".\r\n":
                            break
                        lines.append(line)
                    self.messages.append(b"".join(lines))
                    if action == "data_rejected":
                        send("554 5.7.1 Message rejected")
                    else:
                        send("250 2.0.0 Ok: queued as PARITY")
                elif verb == "RSET":
                    send("250 2.0.0 Ok")
                elif verb == "QUIT":
                    send("221 2.0.0 Bye")
                    return
                else:
                    send("502 5.5.2 Command not recognized")
        except OSError:
            pass
        finally:
            reader.close()
            connection.close()

    def close(self) -> None:
        self._listener.close()


# --------------------------------------------------------------------- IMAP


def _internaldate(now: dt.datetime) -> str:
    months = [
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    ]
    return f"{now.day:02d}-{months[now.month - 1]}-{now.year} {now:%H:%M:%S} +0000"


class ImapFake:
    """A plain IMAP4rev1 server with UIDPLUS; per connection one action:
    ``found`` (one recent message, UID 5), ``empty`` (no match) or
    ``select_no`` (``NO`` on SELECT)."""

    def __init__(self, actions: list[str]) -> None:
        self.script = Script()
        self.script.set("conn", actions)
        self._listener = socket.create_server(("127.0.0.1", 0), backlog=128)
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                connection, _ = self._listener.accept()
            except OSError:
                return
            action = self.script.next("conn")
            threading.Thread(
                target=self._serve, args=(connection, action), daemon=True
            ).start()

    def _serve(self, connection: socket.socket, action: str) -> None:
        connection.settimeout(10)
        reader = connection.makefile("rb")

        def send(line: str) -> None:
            connection.sendall(line.encode() + b"\r\n")

        try:
            send("* OK [CAPABILITY IMAP4rev1 UIDPLUS] parity ready")
            while True:
                raw = reader.readline()
                if not raw:
                    return
                parts = raw.decode("ascii", "replace").strip().split(" ")
                if len(parts) < 2:
                    send("* BAD empty command")
                    continue
                tag, verb = parts[0], parts[1].upper()
                sub = parts[2].upper() if len(parts) > 2 else ""
                if verb == "CAPABILITY":
                    send("* CAPABILITY IMAP4rev1 UIDPLUS")
                    send(f"{tag} OK CAPABILITY completed")
                elif verb == "LOGIN":
                    send(f"{tag} OK LOGIN completed")
                elif verb in ("SELECT", "EXAMINE"):
                    if action == "select_no":
                        send(f"{tag} NO Mailbox does not exist")
                        continue
                    send("* FLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft)")
                    send("* 1 EXISTS")
                    send("* 0 RECENT")
                    send("* OK [UIDVALIDITY 1] UIDs valid")
                    send(f"{tag} OK [READ-WRITE] SELECT completed")
                elif verb == "UID" and sub == "SEARCH":
                    send("* SEARCH 5" if action == "found" else "* SEARCH")
                    send(f"{tag} OK SEARCH completed")
                elif verb == "UID" and sub == "FETCH":
                    now = dt.datetime.now(dt.UTC)
                    send(f'* 1 FETCH (UID 5 INTERNALDATE "{_internaldate(now)}")')
                    send(f"{tag} OK FETCH completed")
                elif verb == "UID" and sub == "STORE":
                    send("* 1 FETCH (UID 5 FLAGS (\\Deleted))")
                    send(f"{tag} OK STORE completed")
                elif verb == "UID" and sub == "EXPUNGE":
                    send("* 1 EXPUNGE")
                    send(f"{tag} OK EXPUNGE completed")
                elif verb == "NOOP":
                    send(f"{tag} OK NOOP completed")
                elif verb == "LOGOUT":
                    send("* BYE parity logging out")
                    send(f"{tag} OK LOGOUT completed")
                    return
                else:
                    send(f"{tag} BAD unknown command")
        except OSError:
            pass
        finally:
            reader.close()
            connection.close()

    def close(self) -> None:
        self._listener.close()


# ---------------------------------------------------------------------- DNS


def dns_answer(message: dns.message.Message, action: Any) -> bytes:
    """The response to ``message``: ``{"a": [ips]}``, ``"nxdomain"`` or
    ``"servfail"``."""
    response = dns.message.make_response(message)
    response.flags |= dns.flags.RA
    question = message.question[0]
    if action == "nxdomain":
        response.set_rcode(dns.rcode.NXDOMAIN)
    elif action == "servfail":
        response.set_rcode(dns.rcode.SERVFAIL)
    else:
        addresses = action.get("a", [])
        if addresses:
            response.answer.append(
                dns.rrset.from_text_list(question.name, 60, "IN", "A", addresses)
            )
    return response.to_wire()


class DnsFake:
    """One UDP + TCP nameserver on ``address``:53 for both agents; scripts
    keyed by the lower-case query name (without the trailing dot)."""

    def __init__(self, address: str, port: int = 53) -> None:
        self.script = Script()
        self.address = address
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._udp.bind((address, port))
            self._tcp.bind((address, port))
        except OSError:
            self._udp.close()
            self._tcp.close()
            raise
        self._tcp.listen()
        threading.Thread(target=self._serve_udp, daemon=True).start()
        threading.Thread(target=self._serve_tcp, daemon=True).start()

    def _respond(self, wire: bytes) -> bytes | None:
        try:
            message = dns.message.from_wire(wire)
        except Exception:  # noqa: BLE001 - a fake ignores garbage
            return None
        if not message.question:
            return None
        name = message.question[0].name.to_text().rstrip(".").lower()
        action = self.script.next(name)
        if action is None:
            action = "nxdomain"
        return dns_answer(message, action)

    def _serve_udp(self) -> None:
        while True:
            try:
                wire, peer = self._udp.recvfrom(4096)
            except OSError:
                return
            answer = self._respond(wire)
            if answer is not None:
                self._udp.sendto(answer, peer)

    def _serve_tcp(self) -> None:
        while True:
            try:
                connection, _ = self._tcp.accept()
            except OSError:
                return
            with connection:
                connection.settimeout(5)
                try:
                    length = struct.unpack("!H", connection.recv(2))[0]
                    wire = b""
                    while len(wire) < length:
                        chunk = connection.recv(length - len(wire))
                        if not chunk:
                            break
                        wire += chunk
                    answer = self._respond(wire)
                    if answer is not None:
                        connection.sendall(struct.pack("!H", len(answer)) + answer)
                except (OSError, struct.error):
                    pass

    def close(self) -> None:
        self._udp.close()
        self._tcp.close()


# --------------------------------------------------------------------- ping

FAKE_PING = r'''#!{python}
"""Fake ping for the parity harness: scripted per target address."""
import json, os, sys, fcntl
state = os.environ["NYXMON_PARITY_PING_STATE"]
address = sys.argv[-1]
with open(os.path.join(state, "lock"), "a+") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    with open(os.path.join(state, "scripts.json")) as f:
        script = json.load(f).get(address, ["unreachable"])
    counter = os.path.join(state, "count-" + address)
    index = int(open(counter).read()) if os.path.exists(counter) else 0
    with open(counter, "w") as f:
        f.write(str(index + 1))
action = script[min(index, len(script) - 1)]
print("PING %s (%s): 56 data bytes" % (address, address))
if action == "ok":
    print("64 bytes from %s: icmp_seq=0 ttl=64 time=1.250 ms" % address)
    print("")
    print("--- %s ping statistics ---" % address)
    print("1 packets transmitted, 1 packets received, 0.0% packet loss")
    sys.exit(0)
print("From 192.0.2.254 icmp_seq=0 Destination Host Unreachable")
print("")
print("--- %s ping statistics ---" % address)
print("1 packets transmitted, 0 packets received, 100.0% packet loss")
sys.exit(1)
'''
