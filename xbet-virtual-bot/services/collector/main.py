"""collector — the only service that talks to 1xbet.

Responsibility, and nothing more: poll the bulk feed for the target
league(s), enrich each match with odds via a detail call (throttled per
match, and skipped entirely once a match is finished and has been captured
once), normalize into MatchSnapshot, publish. It has no notion of
"discovered" / "half-time" / "finished" transitions — that judgment belongs
to the aggregator, which is the only other service allowed to read a
snapshot's meaning rather than just its fields.

Run standalone:
    python -m services.collector.main
"""
from __future__ import annotations

import asyncio
import signal
import time

from services.collector.normalize import is_target_league, normalize_snapshot
from services.collector.xbet_client import XbetClient
from shared.bus import EventBus
from shared.config import load_config
from shared.events import MoneylineOdds, TotalLine
from shared.logging import get_logger


async def run() -> None:
    config = load_config()
    log = get_logger("collector", config.log_dir)

    client = XbetClient(config.api_base, config.http_timeout_seconds)
    bus = EventBus(config.redis_url)
    await bus.connect()

    log.info(
        f"starting — sport_id={config.sport_id} league_filter={config.league_name_filter!r} "
        f"bulk_poll={config.bulk_poll_seconds}s detail_poll={config.detail_poll_seconds}s"
    )

    last_detail_fetch: dict[int, float] = {}
    detail_finalized: dict[int, bool] = {}
    # Odds only come from the (throttled) per-match detail call, not the
    # bulk feed — but a goal can be detected on a poll where that call
    # wasn't due yet, and a snapshot with no odds on it makes the display's
    # live scorecard silently drop its Total O/U box for that update. So the
    # most recent odds captured for a match are kept here and reattached to
    # every snapshot for that match until a fresher detail call replaces
    # them — "a few seconds stale" beats "blank."
    last_known_odds: dict[int, tuple[MoneylineOdds | None, list[TotalLine]]] = {}

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async def fetch_detail_snapshot(match_id: int):
        detail_raw = await client.get_match_detail(match_id)
        last_detail_fetch[match_id] = time.time()
        if not detail_raw:
            return None
        snap = normalize_snapshot(detail_raw)
        if snap.status == "finished":
            detail_finalized[match_id] = True
        return snap

    while not stop.is_set():
        cycle_start = time.time()
        try:
            bulk = await client.get_bulk_feed(config.sport_id)
            targets = [r for r in bulk if is_target_league(r, config.sport_id, config.league_name_filter)]
            target_ids = {r["I"] for r in targets}

            # Forget bookkeeping for matches that have both finished and
            # aged out of the bulk feed — nothing left to ever refetch.
            for match_id in list(detail_finalized):
                if match_id not in target_ids:
                    detail_finalized.pop(match_id, None)
                    last_detail_fetch.pop(match_id, None)
                    last_known_odds.pop(match_id, None)

            needing_detail = [
                raw["I"]
                for raw in targets
                if not detail_finalized.get(raw["I"])
                and time.time() - last_detail_fetch.get(raw["I"], 0) >= config.detail_poll_seconds
            ]
            detail_results = {}
            if needing_detail:
                fetched = await asyncio.gather(
                    *(fetch_detail_snapshot(mid) for mid in needing_detail),
                    return_exceptions=True,
                )
                for match_id, result in zip(needing_detail, fetched):
                    if isinstance(result, Exception):
                        log.warning(f"detail fetch failed for match {match_id}: {result!r}")
                    elif result is not None:
                        detail_results[match_id] = result
                        if result.moneyline is not None or result.totals:
                            last_known_odds[match_id] = (result.moneyline, result.totals)

            for raw in targets:
                match_id = raw["I"]
                snapshot = detail_results.get(match_id)
                if snapshot is None:
                    # No fresh detail call this poll — still attach whatever
                    # odds we last captured for this match rather than
                    # publishing blank ones (see the note on
                    # `last_known_odds` above).
                    snapshot = normalize_snapshot(raw)
                    cached = last_known_odds.get(match_id)
                    if cached is not None:
                        snapshot = snapshot.model_copy(update={"moneyline": cached[0], "totals": cached[1]})
                await bus.publish(config.channel_snapshots, snapshot)

        except Exception as err:  # noqa: BLE001 — one bad poll must not kill the loop
            log.error(f"poll error: {err}")

        elapsed = time.time() - cycle_start
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(0.1, config.bulk_poll_seconds - elapsed))
        except asyncio.TimeoutError:
            pass

    log.info("shutting down...")
    await client.aclose()
    await bus.close()


if __name__ == "__main__":
    asyncio.run(run())
