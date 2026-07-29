"""ZeroMQ SUB helper for tick consumers."""

from __future__ import annotations

import json
from typing import Any, Iterator

import zmq

from ingestion.zmq_pub import DEFAULT_ENDPOINT, TICK_TOPIC


def iter_ticks(
    endpoint: str = DEFAULT_ENDPOINT,
    *,
    stop_event: Any | None = None,
    recv_timeout_ms: int = 1000,
) -> Iterator[dict[str, Any]]:
    """
    Yield tick dicts from the PUB socket until ``stop_event`` is set.

    Connects (does not bind) so multiple worker processes can share one streamer.
    """
    context = zmq.Context.instance()
    socket = context.socket(zmq.SUB)
    socket.setsockopt(zmq.SUBSCRIBE, TICK_TOPIC)
    socket.setsockopt(zmq.RCVTIMEO, recv_timeout_ms)
    socket.setsockopt(zmq.LINGER, 0)
    socket.connect(endpoint)

    try:
        while stop_event is None or not stop_event.is_set():
            try:
                _topic, payload = socket.recv_multipart()
            except zmq.Again:
                continue
            try:
                tick = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(tick, dict):
                yield tick
    finally:
        socket.close(linger=0)
