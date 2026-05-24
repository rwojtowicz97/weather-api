# weather-api

Analiza pogody z ostatniego pół roku dla polskich miast na podstawie danych z Open-Meteo Historical Weather API.

##

Dokumentacja: https://open-meteo.com/en/docs

## Uruchomienie

```bash
uv run python main.py --input pl172.json --output results.json --concurrency 10
```

### Parametry CLI

| Parametr        | Opis                                                                  | Wymagany |
|-----------------|-----------------------------------------------------------------------|----------|
| `--input`       | Plik wejściowy JSON z listą miast                                     | tak      |
| `--output`      | Ścieżka do pliku wynikowego JSON                                      | tak      |
| `--concurrency` | Maksymalna liczba równoległych requestów do API                       | nie      |
| `--source`      | Źródło danych: `archive` (domyślne, 180 dni) lub `forecast` (~90 dni) | nie      |

## Założenia projektowe

### 1. Wybór API i endpointu

Wykorzystywany jest **Historical Weather API** Open-Meteo:

```
https://archive-api.open-meteo.com/v1/archive
```

Endpoint zwraca historyczne dane w przeciwieństwie do `/v1/forecast`, który obsługuje prognozę z ostatnich ~3 miesięcy.

Odpowiedź na request za 180 dni z `/v1/forecast`:
```
curl 'https://api.open-meteo.com/v1/forecast?latitude=41.4595&longitude=-81.6449&hourly=temperature_2m&past_days=180&forecast
_days=1' | jq
{
  "error": true,
  "reason": "Past days is invalid. Allowed range 0 to 93. Given 180."
}
```

**Parametry zapytania:**

| Parametr     | Wartość                                   | Uzasadnienie                                                       |
|--------------|-------------------------------------------|--------------------------------------------------------------------|
| `latitude`   | z `pl172.json`                            | współrzędne miasta                                                 |
| `longitude`  | z `pl172.json`                            | współrzędne miasta                                                 |
| `start_date` | `end_date - 179 dni`                      | 180-dniowe okno                                                    |
| `end_date`   | `today`                                   |                                                                    |
| `daily`      | `weather_code,temperature_2m_mean`        | dokładnie te metryki, których wymaga analiza                       |
| `timezone`   | `auto`                                    | "doba" definiowana w lokalnej strefie miasta                       |

#### Tryb `forecast` (parametr `--source`)

Forecast API (`https://api.open-meteo.com/v1/forecast`) zwraca **identyczną strukturę** odpowiedzi (`daily.time`, `daily.weather_code`, `daily.temperature_2m_mean`), ale obsługuje maksymalnie ~92 dni wstecz. Dodaliśmy go jako alternatywne źródło (`--source forecast`, okno 90 dni) z dwóch powodów:

- **Odporność na awarie** — gdy Historical API jest niedostępne (np. chwilowy outage hosta `archive-api.open-meteo.com`), program nadal działa na krótszym oknie bez zmian w kodzie analizy.
- **Tani benchmark/testy** — krótsze okno = szybsze odpowiedzi, wygodne do wielokrotnych przebiegów narzędzia wydajnościowego.

Domyślne i właściwe dla zadania źródło to `archive` (pełne 180 dni). Pole `metadata.source` w `results.json` zapisuje, które źródło wygenerowało dany wynik.

https://status.open-meteo.com/

### 2. Zakres dat - "ostatnie pół roku"

Pół roku interpretowane jest jako **180 dni**, nie kalendarzowe 6 miesięcy. Ponieważ wtedy wynik jest deterministyczny i niezależny od długości miesięcy.

### 3. Liczba zapytań do API

**1 request na miasto** (172 requesty na pełny przebieg). Open-Meteo zwraca całe okno dat w jednym wywołaniu.

### 4. Model współbieżności

- **`asyncio` + `aiohttp`** - natywny async I/O zamiast wątków, bo zadanie jest w 100% I/O-bound (czekamy na sieć).
- **`asyncio.Semaphore(concurrency)`** - ogranicza liczbę jednocześnie wykonywanych requestów. `--concurrency` mapuje się 1:1 na rozmiar semafora.

#### Dlaczego nie oficjalny klient `openmeteo-requests`?

Open-Meteo udostępnia oficjalną bibliotekę [`openmeteo-requests`](https://pypi.org/project/openmeteo-requests/). Świadomie z niej **nie** korzystam:

- **Konflikt z `--concurrency`** - tracimy kontrole nad liczbą równoległych requestów, klient byłby lepszy do  operacji batchowych.
- **Mniej zależności** - klient korzysta z `niquests` + `flatbuffers`, a do wygodnego czytania danych `numpy`/`pandas`.
- **Przejrzystość** - Nie musimy parsować wyjścia z `numpy`/`pandas` do jsona.
- **Własna obsługa błędów** - możemy sami zarządzać błędami.

Dla produkcyjnego pipeline'u wybór byłby odwrotny.

### 5. Obsługa błędów i ponawianie

- HTTP 429 (rate limit) i błędy sieciowe (`aiohttp.ClientError`, `asyncio.TimeoutError`) - **retry z exponential backoff**, maks. 3 próby.
- HTTP 4xx (poza 429) - brak retry, miasto pomijane z logiem ostrzeżenia.
- Miasta, dla których nie udało się pobrać danych, są odnotowane w wyniku w polu `failed_cities`, ale nie blokują pozostałej analizy.

### 6. Agregacja wyników

Dla każdego miasta liczymy:

- `avg_temperature_c` - średnia arytmetyczna z `temperature_2m_mean` po dniach, z pominięciem `null`.
- `fog_days` - liczba dni z `weather_code == 45`.
- `clear_sky_days` - liczba dni z `weather_code == 0`.

**Brak zjawiska = `null`.** Jeśli żadne miasto nie odnotowało danego zjawiska (maksimum `fog_days` lub `clear_sky_days` wynosi 0), w wyniku zwracamy `null` zamiast wskazywać przypadkowe pierwsze miasto z zerowym licznikiem - taki "zwycięzca" byłby mylący. 

### 7. Struktura `results.json`

```json
{
  "metadata": {
    "source": "archive",
    "start_date": "2025-11-25",
    "end_date": "2026-05-23",
    "cities_analyzed": 172,
    "cities_failed": 0
  },
  "highest_avg_temperature": {
    "city": "...",
    "avg_temperature_c": 12.3
  },
  "most_frequent_fog": {
    "city": "...",
    "fog_days": 45
  },
  "most_frequent_clear_sky": {
    "city": "...",
    "clear_sky_days": 120
  },
  "failed_cities": []
}
```

Metadane są dodane, żeby wynik był samowyjaśniający (źródło + okno, z którego policzony) i reprodukowalny.

### 8. Format `pl172.json`

Plik zawiera 172 obiekty z polami: `city`, `lat`, `lng`, `country`, `iso2`, `admin_name`, `capital`, `population`, `population_proper`. `lat` i `lng` są stringami - w programie konwertowane na `float`.

## Narzędzie analizy wydajności

```bash
uv run python benchmark.py --input pl172.json --concurrency-levels 1,5,10,20,50 --sample 30 --source forecast
```

Mierzy czas pobrania danych dla próbki miast (`--sample`) przy kolejnych poziomach `--concurrency` i wylicza przepustowość (req/s). Importuje rdzeń z `main.py`, więc mierzy realną pracę async bez narzutu uruchamiania interpretera. `--sample` realizuje wymóg "przykładu na **wybranych** danych wejściowych" i ogranicza liczbę wywołań API.

### Przykładowy wynik (30 miast, `--source forecast`)

```
concurrency | time (s) |  req/s | failed
--------------------------------------------
          1 |     1.54 |  19.48 |      0
          5 |     0.43 |  69.80 |      0
         10 |     0.32 |  94.99 |      0
         20 |     1.32 |  22.78 |      0
         50 |     1.67 |  17.93 |      0
```

## Zależności

- `aiohttp` - async HTTP client
- Python ≥ 3.11 (dla `asyncio.TaskGroup` i nowszego API)

Konfiguracja środowiska przez `uv` (`pyproject.toml`).
