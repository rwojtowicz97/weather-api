#!/usr/bin/python3

import argparse
from dataclasses import dataclass
from pathlib import Path
import json
from datetime import date, timedelta
import asyncio
import aiohttp
import sys
from operator import attrgetter

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

SOURCES: dict[str, tuple[str, int]] = {
    "archive": (ARCHIVE_URL, 180),
    "forecast": (FORECAST_URL, 90),
}

FOG_CODE = 45
CLEAR_SKY_CODE = 0
MAX_RETRIES = 3
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
REQUEST_TIMEOUT = 60

@dataclass(frozen=True)
class City:
    name: str
    lat: float
    lng: float

@dataclass
class CityStats:
    city: str
    avg_temperature_c: float | None
    fog_days: int
    clear_sky_days: int

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze weather from Open-Meteo for last 180 days")
    parser.add_argument("--input",required=True, type=Path, help="Path to input file with cities that you want to analyze")
    parser.add_argument("--output", required=True, type=Path, help="Path for output file with results")
    parser.add_argument("--concurrency", type=int, help="Amount of concurrent requests to Open-Meteo", default=1)
    parser.add_argument(
        "--source",
        choices=tuple(SOURCES),
        default="archive",
        type=str,
        help="Source of data: archive(180 days) or forecast(90 days)",
    )

    args = parser.parse_args()

    return args

def load_cities(path: Path) -> list[City]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        sys.exit(f"[error] cannot read input file {path}: {exc}")

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        sys.exit(f"[error] {path} is not valid JSON: {exc}")

    if not isinstance(raw, list):
        sys.exit(f"[error] {path}: expected a JSON array of cities, got {type(raw).__name__}")
    if not raw:
        sys.exit(f"[error] {path}: city list is empty")

    cities = []
    for i, city in enumerate(raw):
        if not isinstance(city, dict):
            sys.exit(f"[error] {path}: record #{i} is not an object")
        try:
            name = str(city["city"])
            lat = float(city["lat"])
            lng = float(city["lng"])
        except KeyError as exc:
            sys.exit(f"[error] {path}: record #{i} is missing required field {exc}")
        except (TypeError, ValueError) as exc:
            sys.exit(f"[error] {path}: record #{i} ({city.get('city', 'empty name')}) has invalid lat/lng: {exc}")
        cities.append(City(name=name, lat=lat, lng=lng))
    return cities

def date_window(window_days: int) -> tuple[date, date]:
    end = date.today()
    start = end - timedelta(days=window_days-1)
    return start, end

def aggregate(city: City, daily: dict) -> CityStats:
    daily_means = daily.get("temperature_2m_mean", [])

    temps = []
    for temp in daily_means:
        if temp is not None: 
            temps.append(temp)
    codes = daily.get("weather_code", [])

    if temps:
        avg = round(sum(temps) / len(temps), 2)
    else:
        avg = None

    fog_days = codes.count(FOG_CODE)
    clear_sky_days = codes.count(CLEAR_SKY_CODE)

    return CityStats(city.name, avg, fog_days, clear_sky_days)

async def fetch_all(cities: list[City], concurrency: int, url: str, start: date, end: date, *, quiet: bool = False) -> tuple[list[CityStats], list[str]]:
    semaphore = asyncio.Semaphore(concurrency)
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    total = len(cities)
    done = 0

    async def worker(city: City) -> CityStats | None:
        nonlocal done
        result = await fetch_city(session, semaphore, url, city, start, end, quiet=quiet)
        done += 1
        if not quiet:
            print(f"\nprogress: [{done}/{total}]")
        return result
    
    async with aiohttp.ClientSession(timeout=timeout) as session:
        results = await asyncio.gather(*(worker(c) for c in cities))

    stats: list[CityStats] = []
    failed: list[str] = []
    for city, res in zip(cities, results):
        if res is None:
            failed.append(city.name)
        else:
            stats.append(res)

    return stats, failed
    
async def fetch_city(
    session: aiohttp.ClientSession, 
    semaphore: asyncio.Semaphore,
    url: str,
    city: City,
    start: date,
    end: date,
    *,
    quiet: bool = False,
) -> CityStats | None:
    params = {
        "latitude": city.lat,
        "longitude": city.lng,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": "weather_code,temperature_2m_mean",
        "timezone": "auto",
    }

    async with semaphore:
        for attempt in range(MAX_RETRIES):
            try:
                async with session.get(url=url, params=params) as resp:
                    if not quiet:
                        print(f"[info] getting data for: `{city.name}`", file=sys.stderr)
                    if resp.status == 200:
                        data = await resp.json()
                        return aggregate(city, data["daily"])
                    if resp.status in RETRYABLE_STATUS:
                        await asyncio.sleep(2**attempt)
                        continue
                    body = (await resp.text())
                    print(f"[warn] {city.name}: HTTP {resp.status}: {body}", file=sys.stderr)
                    return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt == MAX_RETRIES - 1:
                    print(f"[warn] {city.name}: {exc!r}", file=sys.stderr)
                    return None
                await asyncio.sleep(2**attempt)
        print(f"[warn] {city.name}: couldn't get data after {MAX_RETRIES} tries", file=sys.stderr)
        return None

def build_results(
    stats: list[CityStats],
    failed: list[str],
    start: date,
    end: date,
    source: str,
) -> dict:
    city_stats_with_temp = []

    for s in stats:
        if s.avg_temperature_c is not None:
            city_stats_with_temp.append(s)

    hottest = max(city_stats_with_temp, key=attrgetter("avg_temperature_c"), default=None)
    foggiest = max(stats, key=attrgetter("fog_days"), default=None)
    clearest = max(stats, key=attrgetter("clear_sky_days"), default=None)

    return {
        "metadata": {
            "source": source,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "cities_analyzed": len(stats),
            "cities_failed": len(failed),
        },
        "highest_avg_temperature": (
            {"city": hottest.city, "avg_temperature_c": hottest.avg_temperature_c}
            if hottest else None
        ),
        "most_frequent_fog": (
            {"city": foggiest.city, "fog_days": foggiest.fog_days}
            if foggiest and foggiest.fog_days > 0 else None
        ),
        "most_frequent_clear_sky": (
            {"city": clearest.city, "clear_sky_days": clearest.clear_sky_days}
            if clearest and clearest.clear_sky_days > 0 else None
        ),
        "failed_cities": failed,
    }

def main():
    args = parse_args()
    cities = load_cities(args.input)
    url, window_days = SOURCES[args.source]
    start, end = date_window(window_days)
    stats, failed = asyncio.run(fetch_all(cities, args.concurrency, url, start, end))
    results = build_results(stats, failed, start, end, args.source)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Results file: {args.output} | failed cities: {len(failed)}", file=sys.stdout)


if __name__ == "__main__":
    main()