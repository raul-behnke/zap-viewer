"""Read-only media size report. Run: python -m app.mediastat [--sample N]

Local media lives in the WAHA container's /tmp and is wiped on every
restart, so disk usage can't be measured directly. This samples real
messages, asks WAHA for each one's size through the same host-normalized,
pinned path fetch_media uses (HEAD, falling back to a ranged GET, falling
back to a full GET only as a last resort), and extrapolates a bytes/day
rate so a retention window can be chosen for the 9 GB R2 cap.

Never writes to the database and never deletes media. Skips messages whose
media is already gone (404/410) rather than aborting the whole sample.
"""

import argparse
import asyncio
import statistics
from dataclasses import dataclass, field
from typing import Any

import httpx
from psycopg import AsyncConnection

from app.config import settings
from app.waha import Waha, WahaError

FAMILIES = ("image", "video", "audio", "document")


def mime_family(mime: str | None) -> str:
    mt = (mime or "").split(";")[0].strip().lower()
    for family in ("image", "video", "audio"):
        if mt.startswith(f"{family}/"):
            return family
    return "document"


@dataclass
class Sample:
    message_id: str
    ts: Any
    mime: str | None
    media_url: str


@dataclass
class Result:
    ok_sizes: dict[str, list[int]] = field(default_factory=lambda: {f: [] for f in FAMILIES})
    failures: dict[str, int] = field(default_factory=dict)

    @property
    def attempted(self) -> int:
        return sum(len(v) for v in self.ok_sizes.values()) + sum(self.failures.values())

    @property
    def succeeded(self) -> int:
        return sum(len(v) for v in self.ok_sizes.values())


def pick_evenly_strided(rows: list[tuple], n: int) -> list[tuple]:
    """Evenly-strided sample across `rows` (already ordered by ts).

    Chosen over random.sample because it deterministically spreads the
    sample across the whole retention window (oldest to newest) instead of
    risking a random draw clustered in one era of the mirror -- and it's
    reproducible, which matters for a report someone will sanity-check.
    """
    total = len(rows)
    if total <= n:
        return rows
    stride = total / n
    indices = sorted({int(i * stride) for i in range(n)})
    return [rows[i] for i in indices]


async def fetch_rows(conn: AsyncConnection) -> list[tuple]:
    cur = await conn.execute(
        """
        SELECT id, ts, media_mime, media_url
        FROM messages
        WHERE has_media AND media_url IS NOT NULL
        ORDER BY ts ASC
        """
    )
    return await cur.fetchall()


async def probe(waha: Waha, samples: list[Sample], result: Result) -> None:
    for s in samples:
        try:
            size = await waha.media_size(s.media_url)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            reason = "gone (404/410)" if code in (404, 410) else f"http {code}"
            result.failures[reason] = result.failures.get(reason, 0) + 1
            continue
        except (WahaError, httpx.HTTPError) as exc:
            reason = f"{type(exc).__name__}"
            result.failures[reason] = result.failures.get(reason, 0) + 1
            continue
        result.ok_sizes[mime_family(s.mime)].append(size)


def _pct(sorted_values: list[int], pct: float) -> int:
    if not sorted_values:
        return 0
    idx = min(len(sorted_values) - 1, int(round(pct * (len(sorted_values) - 1))))
    return sorted_values[idx]


def report(
    result: Result,
    sample_target: int,
    total_media_rows: int,
    oldest_ts: Any,
    newest_ts: Any,
    cap_bytes: int,
) -> str:
    lines: list[str] = []
    lines.append(f"sampled {result.attempted} of {sample_target} requested")
    lines.append(f"{result.succeeded} of {result.attempted} probes succeeded")

    if result.failures:
        lines.append("failures:")
        for reason, count in sorted(result.failures.items()):
            lines.append(f"  {reason}: {count}")

    if result.succeeded == 0:
        lines.append("")
        lines.append(
            "0 of {} samples succeeded -- no size data, cannot extrapolate. "
            "Check that media fetch actually works (see Job 1) before "
            "trusting this report.".format(result.attempted)
        )
        return "\n".join(lines)

    lines.append("")
    lines.append("per mimetype family:")
    grand_total_bytes = 0
    grand_total_count = 0
    for family in FAMILIES:
        sizes = sorted(result.ok_sizes[family])
        if not sizes:
            continue
        n = len(sizes)
        total = sum(sizes)
        grand_total_bytes += total
        grand_total_count += n
        lines.append(
            f"  {family}: n={n} mean={statistics.mean(sizes):,.0f}B "
            f"median={statistics.median(sizes):,.0f}B "
            f"p90={_pct(sizes, 0.9):,}B total={total:,}B"
        )

    lines.append("")
    span = newest_ts - oldest_ts
    span_days = max(span.total_seconds() / 86400, 1e-9)
    mean_size_overall = grand_total_bytes / grand_total_count
    est_total_bytes_all_media = mean_size_overall * total_media_rows
    bytes_per_day = est_total_bytes_all_media / span_days

    lines.append("extrapolation (arithmetic shown, sanity-check it):")
    lines.append(
        f"  mean size across {grand_total_count} successful samples "
        f"= {grand_total_bytes:,}B / {grand_total_count} "
        f"= {mean_size_overall:,.0f} B/message"
    )
    lines.append(
        f"  media rows in db = {total_media_rows}, oldest={oldest_ts}, "
        f"newest={newest_ts}, span = {span_days:.2f} days"
    )
    lines.append(
        f"  estimated total media bytes = {mean_size_overall:,.0f} B/msg "
        f"* {total_media_rows} msgs = {est_total_bytes_all_media:,.0f} B"
    )
    lines.append(
        f"  bytes/day = {est_total_bytes_all_media:,.0f} B / {span_days:.2f} days "
        f"= {bytes_per_day:,.0f} B/day"
    )
    if bytes_per_day > 0:
        days_in_cap = cap_bytes / bytes_per_day
        lines.append(
            f"  days of media that fit in {cap_bytes:,} B (9 GB) = "
            f"{cap_bytes:,} B / {bytes_per_day:,.0f} B/day = {days_in_cap:,.1f} days"
        )
    lines.append("")
    lines.append(
        f"note: this estimate is only as good as its sample "
        f"({result.succeeded} of {result.attempted} probes succeeded, "
        f"{result.succeeded} of {total_media_rows} total media rows)."
    )
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=200)
    args = parser.parse_args()

    conn: AsyncConnection[Any] = await AsyncConnection.connect(
        settings.database_url, autocommit=True
    )
    try:
        rows = await fetch_rows(conn)
    finally:
        await conn.close()

    if not rows:
        print("no media messages in the database")
        return

    picked = pick_evenly_strided(rows, args.sample)
    samples = [
        Sample(message_id=r[0], ts=r[1], mime=r[2], media_url=r[3]) for r in picked
    ]

    waha = Waha(settings.waha_url, settings.waha_api_key, settings.waha_session)
    result = Result()
    try:
        await probe(waha, samples, result)
    finally:
        await waha.aclose()

    print(
        report(
            result,
            sample_target=len(samples),
            total_media_rows=len(rows),
            oldest_ts=rows[0][1],
            newest_ts=rows[-1][1],
            cap_bytes=9 * 1024 * 1024 * 1024,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
