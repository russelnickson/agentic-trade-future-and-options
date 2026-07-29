"""ZeroMQ PUB socket for broadcasting raw WebSocket ticks."""

from __future__ import annotations

import atexit
import json
import threading
from typing import Any

import zmq

DEFAULT_ENDPOINT = "tcp://127.0.0.1:5555"
TICK_TOPIC = b"tick"

_lock = threading.Lock()
_context: zmq.Context | None = None
_socket: zmq.Socket | None = None


def _get_publisher(endpoint: str = DEFAULT_ENDPOINT) -> zmq.Socket:
    global _context, _socket
    with _lock:
        if _socket is not None:
            return _socket
        _context = zmq.Context.instance()
        _socket = _context.socket(zmq.PUB)
        _socket.setsockopt(zmq.LINGER, 0)
        _socket.bind(endpoint)
        atexit.register(close)
        return _socket


def publish_tick(tick: dict[str, Any], *, endpoint: str = DEFAULT_ENDPOINT) -> None:
    """Broadcast a raw WebSocket tick as JSON over the ZeroMQ PUB socket."""
    socket = _get_publisher(endpoint)
    payload = json.dumps(tick, default=str, separators=(",", ":")).encode("utf-8")
    # Multipart: topic + body so subscribers can filter with SUBSCRIBE.
    socket.send_multipart([TICK_TOPIC, payload])


def close() -> None:
    """Tear down the publisher socket (safe to call multiple times)."""
    global _context, _socket
    with _lock:
        if _socket is not None:
            _socket.close(linger=0)
            _socket = None
        _context = None
