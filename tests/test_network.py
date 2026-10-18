"""tests/container/network.py's Relay, on the machine: it carries ``CONNECT github.com:443``
end to end to its target and refuses any other host, port or method, recording each."""

import socket
import threading

import pytest

from tests.container import network


@pytest.fixture
def echo():
    """A server on the loopback that sends back what it is sent, and its address."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(4)

    def serve():
        while True:
            try:
                client, _ = server.accept()
            except OSError:
                return
            with client:
                while data := client.recv(4096):
                    client.sendall(data)

    threading.Thread(target=serve, daemon=True).start()
    yield server.getsockname()
    server.close()


@pytest.fixture
def relay(echo):
    made = network.Relay("github.com", echo)
    yield made
    made.close()


def asked(relay, line: bytes, then: bytes = b"") -> bytes:
    with socket.create_connection(("127.0.0.1", relay.port), timeout=10) as client:
        client.sendall(line + b"\r\nHost: x\r\n\r\n")
        answer = client.recv(4096)
        if then and answer.startswith(b"HTTP/1.1 200"):
            client.sendall(then)
            client.shutdown(socket.SHUT_WR)
            back = b""
            while data := client.recv(4096):
                back += data
            return answer + back
        return answer


def test_a_connect_to_github_com_443_is_carried_end_to_end(relay):
    answer = asked(relay, b"CONNECT github.com:443 HTTP/1.1", b"bytes of TLS")
    assert answer.startswith(b"HTTP/1.1 200") and answer.endswith(b"bytes of TLS")
    assert relay.refused == []


@pytest.mark.parametrize(
    "line",
    [
        b"CONNECT example.com:443 HTTP/1.1",
        b"CONNECT github.com:80 HTTP/1.1",
        b"GET http://github.com/ HTTP/1.1",
    ],  # fmt: skip
    ids=["another host", "another port", "another method"],
)
def test_anything_else_is_refused_and_recorded(relay, line):
    answer = asked(relay, line, b"bytes")
    assert answer.startswith(b"HTTP/1.1 403") and b"bytes" not in answer
    assert relay.refused == [line]
