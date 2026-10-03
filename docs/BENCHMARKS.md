# Benchmarks (FAKE transport - not Telegram throughput)

## Pipeline concurrency (64 MiB, 128 parts, fake 20 ms round trip per part)

| concurrency | seconds | parts/s | max in flight |
|---|---|---|---|
| 1 | 2.69 | 48 | 1 |
| 2 | 1.36 | 94 | 2 |
| 4 | 0.69 | 185 | 4 |
| 8 | 0.36 | 355 | 8 |

## Pipeline overhead with zero latency (pure Teloude cost, 64 MiB)

0.05 s  (1396 MiB/s of local read + scheduling + fake copy)

## Speed limiter (16 MiB, 4 parts in flight)

| limit | expected s | measured s |
|---|---|---|
| unlimited | - | 0.03 |
| 10 MB/s | 1.60 | 1.61 |
| 5 MB/s | 3.20 | 3.20 |
| 2 MB/s | 8.00 | 8.00 |

## Retry behaviour (every 5th request fails; base delay 0.05 s, instant sleeps counted)

7 retries, 7 reconnects (single-flight), completed in 0.03 s, longest single backoff chunk 0.05 s

## Checkpoint cost (blocking sink, 100 ms per save) does not stall the pipeline

fast sink 0.03 s vs 100 ms-per-save sink 0.23 s (2 saves)

Checkpoint (de)serialisation of 4000 scattered parts: 1.14 ms each
