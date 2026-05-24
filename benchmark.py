from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from main import SOURCES, City, date_window, fetch_all, load_cities


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, type=Path, help="Path to input file with cities that you want to analyze")
    parser.add_argument(
        "--concurrency-levels",
        default="1,5,10,20,50",
        type=str,
        help="Concurrency levels, example: 1,5,10,20,50",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=30,
        help="Amout of cities used from input file",
    )
    parser.add_argument(
        "--source",
        choices=tuple(SOURCES),
        default="archive",
        type=str,
        help="Source of data: archive(180 days) or forecast(90 days)",
    )

    args = parser.parse_args()

    return args


async def measure(cities: list[City], concurrency: int, url: str, start, end) -> tuple[float, int]:
    t0 = time.perf_counter()
    _, failed = await fetch_all(cities, concurrency, url, start, end, quiet=True)
    return time.perf_counter() - t0, len(failed)


def main():
    args = parse_args()
    levels = [int(x) for x in args.concurrency_levels.split(",")]
    cities = load_cities(args.input)[: args.sample]
    url, window_days = SOURCES[args.source]
    start, end = date_window(window_days)
    n = len(cities)

    print(f"Benchmark: {n} cities (sample) | source={args.source} | time window {start}..{end}")
    print(f"Concurrency levels: {levels}\n")
    print(f"{'concurrency'} | {'time (s)'} | {'req/s'} | {'failed'}")
    print("--------------------------------------------")
    for level in levels:
        elapsed, failed = asyncio.run(measure(cities, level, url, start, end))
        rps = n / elapsed if elapsed > 0 else 0.0
        print(f"{level:>11} | {elapsed:>8.2f} | {rps:>6.2f} | {failed:>6}")


if __name__ == "__main__":
    main()
