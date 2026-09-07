"""Thin async wrapper around Redis pub/sub — the one place that knows how a
pydantic event becomes bytes on the wire and back.

This is the seam that makes the three processes "microservices" rather than
three modules glued together: collector, aggregator and display share no
Python state, only this bus. Any one of them can be killed, restarted, or
replaced with a different implementation (a different language, even)
without the others noticing, as long as it speaks the same channel/JSON
contract from shared/events.py.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

import redis.asyncio as redis
from pydantic import BaseModel

from shared.events import MATCH_EVENT_TYPES, MatchEvent, MatchSnapshot


class EventBus:
    def __init__(self, redis_url: str):
        self._redis_url = redis_url
        self._client: redis.Redis | None = None

    async def connect(self) -> None:
        self._client = redis.from_url(self._redis_url, decode_responses=True)
        await self._client.ping()

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    async def publish(self, channel: str, event: BaseModel) -> None:
        assert self._client, "EventBus.connect() not called"
        await self._client.publish(channel, event.model_dump_json())

    async def subscribe_snapshots(self, channel: str) -> AsyncIterator[MatchSnapshot]:
        async for raw in self._subscribe_raw(channel):
            yield MatchSnapshot.model_validate_json(raw)

    async def subscribe_match_events(self, channel: str) -> AsyncIterator[MatchEvent]:
        async for raw in self._subscribe_raw(channel):
            # Cheap peek at "kind" to pick the right model before validating —
            # this is what lets one channel safely carry five different event
            # shapes without a subscriber having to try/except its way through.
            kind = json.loads(raw).get("kind")
            model = MATCH_EVENT_TYPES.get(kind)
            if model is None:
                continue
            yield model.model_validate_json(raw)

    async def _subscribe_raw(self, channel: str) -> AsyncIterator[str]:
        assert self._client, "EventBus.connect() not called"
        pubsub = self._client.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                yield message["data"]
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
