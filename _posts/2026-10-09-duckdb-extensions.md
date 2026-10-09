---
title: "Making remote data feel local in DuckDB: 19 extensions, 2.2 million downloads"
date: 2026-10-09 00:00:00 -0700
description: "Nineteen DuckDB community extensions for caching, storage, accessibility, IO resilience and observability, and how they compose, with download numbers."
tags: [DuckDB, Extensions, Storage, Caching]
---

DuckDB is very fast on a laptop SSD. Point it at S3, though, and you're dealing with round trips, tail latency, throttling and egress bills. Over the last eighteen months my collaborators and I have shipped 19 community extensions that close that gap, one layer at a time. Together they have been downloaded more than 2.2 million times.

> **Summary.** DuckDB's engine is excellent. Remote storage is the hard part: every query against object storage pays for network latency, flaky requests and egress. Rather than build one large "remote DuckDB", I built small extensions that each fix one problem and can be stacked on top of any DuckDB filesystem.
>
> - **19 extensions in five focus areas:** cache and acceleration, storage and filesystems, accessibility, IO resilience, and observability. All of them are installable with `INSTALL … FROM community`.
> - **At least 2,201,673 downloads** across the archived weekly snapshots from `community-extensions.duckdb.org`, through October 9, 2026. Filling in the weeks the archive skipped puts the total at about 2.6 million.
> - **`cache_httpfs` is the fifth most downloaded community extension**, out of 359 last week and out of 372 over all archived weeks. It has 1.6 million downloads, and 58,798 of them came last week.
> - **The extensions compose.** `query_condition_cache` sits above `cache_httpfs`, which sits above `hedged_request_fs`, which sits above `duckdb_opendalfs`. Each one wraps whatever filesystem is below it, so you choose the layers you need.
> - **Six of them are highlighted below:** `cache_httpfs`, `query_condition_cache` (a predicate cache, up to 314× faster on repeated log queries), `cache_prewarm`, `lance_conversion`, `duckherder` and `duckdb_opendalfs`.

![19 DuckDB community extensions grouped into five focus areas](/assets/images/duckdb-extensions/extension-map.png)

*All 19 extensions, grouped by what they're for. ★ marks the ones highlighted in this post. Source: `duckdb/community-extensions` descriptors; download counts come from the weekly snapshots on `community-extensions.duckdb.org`. See [Methodology](#methodology).*

---

## Why remote storage needs more than one extension

DuckDB's built-in `httpfs` does one job well: it turns an `s3://` or `https://` URL into something DuckDB can read. Production workloads quickly need more than that:

- **Repeated reads cost the full price every time.** A dashboard that refreshes every 30 seconds downloads the same Parquet footers and row groups again and again.
- **Tail latency dominates.** One slow `GET` out of a thousand can hold up a whole scan.
- **Failures and throttling are normal.** Object stores return 503s and rate-limit you, and the defaults rarely match what your workload can tolerate.
- **Not everything is S3.** Teams also keep data on GCS, Azure Blob, Hugging Face, WebDAV and SFTP.
- **You can't tune what you can't see.** "The query is slow" isn't something you can act on until you know whether the time went to opens, globs or reads, and whether the cache hit.

I could have built one monolithic extension. Instead I followed a Unix-style rule: **each extension does one thing, and every filesystem-level extension can wrap any other DuckDB filesystem.** In practice that looks like this:

```sql
LOAD duckdb_opendalfs;     -- talk to GCS / Azure / HF / SFTP
LOAD hedged_request_fs;    -- cut tail latency on slow metadata calls
LOAD cache_httpfs;         -- cache blocks, metadata, globs on local disk
CALL cache_httpfs_wrap_cache_filesystem('duckdb_opendalfs');
```

![The extensions arranged as a layered IO stack](/assets/images/duckdb-extensions/storage-stack.png)

*The filesystem-level extensions form a stack. Each layer is optional, and each wrapper accepts any DuckDB-compatible filesystem, including ones I didn't write.*

---

## The numbers

![Per-extension cumulative downloads, log scale](/assets/images/duckdb-extensions/downloads-by-extension.png)

*Cumulative downloads for each extension, from its first community release to October 9, 2026, summed over the weekly snapshots that are archived. The axis is logarithmic. `compression_fs` and `slack` don't show up in the stats yet.*

| Extension | Focus area | First release | Downloads (archived weeks) | Last week |
| --- | --- | --- | ---: | ---: |
| **`cache_httpfs`** | Cache | 2025-03 | **1,598,643** | 58,798 |
| `curl_httpfs` | Storage | 2025-09 | 270,239 | 3,170 |
| `observefs` | Observability | 2025-09 | 76,867 | 4,666 |
| `system_stats` | Observability | 2025-12 | 36,705 | 931 |
| `httpfs_timeout_retry` | Resilience | 2026-02 | 29,850 | 1,200 |
| `table_inspector` | Observability | 2026-02 | 28,452 | 1,300 |
| **`cache_prewarm`** | Cache | 2026-02 | **27,240** | 996 |
| `rate_limit_fs` | Resilience | 2026-02 | 26,884 | 951 |
| `hedged_request_fs` | Resilience | 2026-02 | 26,797 | 1,010 |
| `latency_injection_fs` | Resilience | 2026-03 | 25,527 | 953 |
| **`query_condition_cache`** | Cache | 2026-04 | **18,626** | 908 |
| **`duckherder`** | Accessibility | 2025-11 | **10,579** | 848 |
| `query_limiter` | Observability | 2026-07 | 9,439 | 866 |
| **`duckdb_opendalfs`** | Storage | 2026-07 | **8,764** | 1,011 |
| `huggingface` | Accessibility | 2026-08 | 4,884 | 837 |
| **`lance_conversion`** | Accessibility | 2026-09 | **1,832** | 790 |
| `duckdb_object_storage` | Storage | 2026-09 | 345 | 265 |
| `compression_fs` | Storage | 2026-09 | — | — |
| `slack` | Accessibility | 2026-02 | — | — |
| **Total** | | | **2,201,673** | |

The total is a lower bound. The upstream archive skipped about 30 weeks, and interpolating across those gaps brings the total to about **2.6 million**.

![Weekly downloads stacked by focus area](/assets/images/duckdb-extensions/weekly-downloads.png)

*Weekly downloads across all 19 extensions, stacked by focus area. The grey bands are weeks the upstream stats archive doesn't cover; they are missing data, not zero downloads.*

Two things stand out:

1. **The cache layer accounts for most of the downloads.** `cache_httpfs` is consistently in the top five of the whole community catalogue. Remote reads are the bottleneck people feel most.
2. **The long tail adds up.** The IO resilience and observability extensions each get roughly 1,000 downloads a week. None of them is a hit on its own, but together they add more than 260,000 downloads, mostly from people who have already adopted the cache layer and want more control.

---

## Highlights

### ★ `cache_httpfs` — a read cache for remote files

**The problem.** DuckDB re-fetches remote bytes on every query. Dashboards, notebooks and BI tools ask the same questions again and again, and each one pays the full network round trip and the egress cost.

**What it does.** `cache_httpfs` wraps `httpfs` (or any DuckDB filesystem) and caches at four levels: **data blocks, file metadata, file handles and glob results**. Blocks can live in memory (LRU) or on local disk (the default), with eviction by LRU or access time, multi-disk support and a reserved-space limit. Reads are split into aligned, configurable chunks and fetched in parallel. A built-in profiler reports cache hit and miss rates and per-operation latency, so you can see what's happening.

```sql
INSTALL cache_httpfs FROM community;
LOAD cache_httpfs;           -- no need to LOAD httpfs

SET cache_httpfs_type = 'on_disk';
SELECT count(*) FROM 's3://my-bucket/events/*.parquet';   -- cold
SELECT count(*) FROM 's3://my-bucket/events/*.parquet';   -- served locally

SELECT * FROM cache_httpfs_cache_access_info_query();     -- hit/miss by entity
```

It is a drop-in replacement for `httpfs`. `SET cache_httpfs_type = 'noop'` turns caching off and gives you plain `httpfs` behaviour back, so you can try it without much risk.

**Traction.** 1.6 million downloads, about 59,000 a week, and fifth in the community catalogue.

<!-- TODO(hao): add one sentence and a chart from `benchmark/README.md` (sequential and random read bar charts) with a concrete speedup number. -->

---

### ★ `query_condition_cache` — a predicate cache for DuckDB

**The problem.** Monitoring dashboards and log investigations run the same `WHERE` clauses over the same tables all day. DuckDB evaluates the predicate over every row group on every run.

**What it does.** The extension remembers **which vectors inside each row group matched a predicate**. On later queries it injects a `ROW_ID`-backed filter so DuckDB skips vectors already known to be empty. The design follows the SIGMOD '24 paper [*Predicate Caching: Query-Driven Secondary Indexing for Cloud Data Warehouses*](https://dl.acm.org/doi/10.1145/3626246.3653395) and [ClickHouse's query condition cache](https://clickhouse.com/blog/introducing-the-clickhouse-query-condition-cache). Uncached row groups pass through unchanged, so correctness never depends on the cache.

```sql
INSTALL query_condition_cache FROM community;
LOAD query_condition_cache;

-- Automatic: the optimizer builds the cache on a miss and uses it on a hit.
SELECT count(*) FROM logs WHERE level = 'ERROR' AND msg LIKE '%timeout%';

-- Or build it yourself for a predicate you already know about.
SELECT * FROM condition_cache_build('logs', 'level = ''ERROR''');
SELECT * FROM condition_cache_info('logs', 'level = ''ERROR''');
```

**Result.** On the [HDFS_v2 log analytics benchmark](https://github.com/logpai/loghub/tree/master/HDFS) (about 71 million log lines), repeated queries ran **up to 314.3× faster**, with the largest gains on selective investigation queries.

<!-- TODO(hao): embed `docs/img/hdfs_log_bench.png` from the repo. -->

---

### ★ `cache_prewarm` — `pg_prewarm` for DuckDB

**The problem.** The first query after a restart is slow. On a fresh container or a just-scaled node, the first user pays to warm the cache.

**What it does.** Inspired by PostgreSQL's `pg_prewarm`, it loads table blocks before the first query arrives. There are three modes: `buffer` (pin blocks into DuckDB's buffer pool), `read` (warm the OS page cache) and `prefetch` (OS readahead hints). Combined with `cache_httpfs`, it also prewarms remote files, including whole glob patterns, into the local disk cache.

```sql
SELECT prewarm('events');                       -- buffer pool
SELECT prewarm('events', 'read', '4GB');        -- OS page cache, size-capped
SELECT prewarm_remote('s3://lake/events/*.parquet');  -- needs cache_httpfs
```

This makes query latency predictable from the first request. It's the kind of feature you only notice when it's missing.

---

### ★ `lance_conversion` — `COPY` any query to Lance

**The problem.** [Lance](https://lancedb.github.io/lance/) is becoming the default format for AI and multimodal data, but getting data into it usually meant exporting Parquet, then running a separate Python job, then building indexes.

**What it does.** It registers Lance as a native `COPY` target. DuckDB does the reading, filtering, joining and casting, then streams the result straight into the Rust Lance writer, with no intermediate Parquet file and no full materialization. Indexes are declared in the same statement:

```sql
COPY (
  SELECT id, category, description, embedding, asset_uri
  FROM read_parquet('s3://lake/catalog/**/*.parquet')
) TO 's3://lake/catalog.lance' (
  FORMAT LANCE,
  PRESERVE_ORDER false,              -- one Lance writer per DuckDB thread
  BLOB_COLUMNS (asset_uri),          -- Blob v2 layout
  SCALAR_INDEX_COLUMNS (id, category),
  VECTOR_INDEX_COLUMNS (embedding),
  TEXT_INDEX_COLUMNS (description)
);
```

It supports create, append and overwrite modes, random sampling (`SAMPLE_PERCENT`, `SAMPLE_ROWS`), S3 credentials taken from DuckDB secrets, and bundled `read_huggingface` and `read_warc` readers. That means "Common Crawl to an indexed Lance dataset" is a single SQL statement.

**Traction.** It's three weeks old and already gets about 800 downloads a week.

---

### ★ `duckherder` — remote and distributed execution over Arrow Flight

**The problem.** DuckDB is in-process by design. Sometimes the data or the compute lives on another machine.

**What it does.** `duckherder` is a DuckDB *storage extension*. You `ATTACH` a remote server, and DDL and DML run there exactly as they would locally, with results streamed back over [Arrow Flight](https://arrow.apache.org/docs/format/Flight.html). On the server, a driver node analyzes the physical plan, partitions it, and sends tasks to worker nodes. Any driver or worker that speaks the duckherder dialect (gRPC stubs plus Arrow Flight) can be plugged in.

```sql
INSTALL duckherder FROM community;
LOAD duckherder;
SELECT duckherder_start_local_server(8815);
ATTACH DATABASE 'localhost:8815' AS dh (TYPE duckherder);
CREATE TABLE dh.events AS SELECT * FROM 's3://lake/events/*.parquet';
SELECT category, count(*) FROM dh.events GROUP BY ALL;
```

It's still a work in progress and not ready for production. It's a personal project, not affiliated with DuckDB Labs. It's also the most ambitious thing on this list: the target design has a single writer and many readers on top of object storage, where `duckdb_object_storage` comes in.

---

### ★ `duckdb_opendalfs` — one extension, many storage backends

**The problem.** Every new storage service otherwise needs its own DuckDB filesystem, with its own URI rules and its own way of handling credentials.

**What it does.** It maps [Apache OpenDAL](https://opendal.apache.org/) onto DuckDB's virtual filesystem. With one extension you get **S3-compatible stores, GCS, Azure Blob, Hugging Face, WebDAV, SFTP and more**, all configured through ordinary DuckDB secrets (`opendal_s3`, `opendal_gcs`, `opendal_hf`, …) with longest-prefix scope matching. Each operation gets a timeout layer and a retry layer with exponential backoff by default.

```sql
INSTALL duckdb_opendalfs FROM community;
LOAD duckdb_opendalfs;

CREATE SECRET prod_gcs (TYPE opendal_gcs, SCOPE 'gcs://analytics', TOKEN '...');
SELECT * FROM read_parquet('gcs://analytics/events.parquet');

-- Add the performance layer:
LOAD cache_httpfs;
CALL cache_httpfs_wrap_cache_filesystem('duckdb_opendalfs');
```

This is where the composable design pays off: `duckdb_opendalfs` handles reaching many backends, and `cache_httpfs` handles speed.

---

## The rest of the stack

### Storage and filesystems

- **`curl_httpfs`** reimplements `httpfs`'s HTTP layer on libcurl and epoll, with **HTTP/2 by default, a TCP connection pool and fully asynchronous IO**. It's 100% compatible with `httpfs`, and you can switch back to httplib with one setting. It has 270,000 downloads and is the second most popular extension on this list.
- **`duckdb_object_storage`** stores **writable `.duckdb` databases, including the WAL, on local or S3-compatible storage through [SlateDB](https://slatedb.io/)**: `ATTACH 'duckdb_objfs://analytics.db'`.
- **`compression_fs`** adds **LZ4, Snappy, Brotli, Bzip2 and XZ** to DuckDB's virtual filesystem, so `read_csv('x.csv.xz')` and `COPY … (COMPRESSION 'xz')` just work.

### IO resilience

All four wrap any filesystem without changing your queries:

- **`hedged_request_fs`**: when a metadata or directory call is slow, it sends a second request and uses whichever answers first. This is the classic [*Tail at Scale*](https://research.google/pubs/the-tail-at-scale/) technique, with configurable thresholds and attempt limits.
- **`httpfs_timeout_retry`**: separate timeout and retry settings for open, read, write, list and stat.
- **`rate_limit_fs`**: GCRA-based rate limits and burst limits, per filesystem and per operation, so you stay under your object store's throttling limits.
- **`latency_injection_fs`**: injects log-normally distributed latency into any operation, so you can benchmark S3-like behaviour on a laptop. Pair it with `rate_limit_fs` and you have a cheap chaos-testing setup.

### Observability and governance

- **`observefs`** records latency histograms and quantiles for every IO operation, per process and per bucket, and shows how DuckDB's external file cache is being used. It works with any registered filesystem, including Azure.
- **`table_inspector`** shows storage internals at the database, table and column level, which helps answer "why is this file 40 GB?" and "why is this column compressing badly?". Built with [@Andrewtangtang](https://github.com/Andrewtangtang).
- **`system_stats`** exposes CPU, memory and disk statistics as table functions (written in Rust and C++).
- **`query_limiter`** rejects a query **before physical execution** if the estimated table scans exceed a row budget you set. It's a guardrail for shared or multi-tenant DuckDB.

### Accessibility

- **`huggingface`** discovers, scans (with projection and filter pushdown), profiles and estimates blob storage for Hugging Face Parquet datasets. It uses `cache_httpfs` for `hf://` paths.
- **`slack`** lets you search Slack messages from SQL.

---

## Built with others

None of this was a solo effort. Thank you to [@peterxcli](https://github.com/peterxcli) (`cache_prewarm`, `observefs`, `query_condition_cache`), [@DouEnergy](https://github.com/DouEnergy) (`cache_httpfs`) and [@Andrewtangtang](https://github.com/Andrewtangtang) (`query_condition_cache`, `table_inspector`). Thanks also to the DuckDB Labs team and the `community-extensions` maintainers, who review and build all of this for every platform on every release.

---

## Try it

Every extension installs the same way:

```sql
INSTALL cache_httpfs FROM community;
LOAD cache_httpfs;
```

If you only try one, start with `cache_httpfs` on a dashboard that queries S3. If you work with logs, try `query_condition_cache`. If you're building AI datasets, try `lance_conversion`.

Issues, benchmarks and pull requests are very welcome at [github.com/dentiny](https://github.com/dentiny). If you're running DuckDB against object storage in production and something still hurts, I'd like to hear about it. That's usually how the next extension starts.

<!-- TODO(hao): add contact links (X / LinkedIn / email), and a short "about me" line (current role, prior work). -->

---

## Methodology

- **Extension list.** Every `extensions/*/description.yml` in [`duckdb/community-extensions`](https://github.com/duckdb/community-extensions) that lists `dentiny` as a maintainer: 19 in total. "First release" is the date the descriptor was first added to that repository.
- **Download counts.** Weekly JSON snapshots from `https://community-extensions.duckdb.org/download-stats-weekly/<year>/<week>.json`, plus `downloads-last-week.json`. There are 76 archived weeks, from 2024-W40 to 2026-W41.
- **Gaps.** The archive is missing 2025-W22 to W37, 2025-W41 to W45, and 2026-W01 to W09. The headline figure (**2,201,673**) adds up only the weeks that exist, so it's a lower bound. Filling each missing week by linear interpolation between the surrounding observed weeks gives **about 2,605,000**. Extensions released inside a gap (for example the February 2026 releases) are undercounted further, because interpolation can't reach back before the first week an extension appears.
- **Ranking.** "Fifth most downloaded" uses the same data for every community extension: fifth of 359 in the latest weekly snapshot (2026-10-09), and fifth of 372 when all archived weeks are summed.
- **Caveats.** A download is an `INSTALL` from the community repository, not a unique user, so CI pipelines and ephemeral containers inflate the numbers for every extension alike. The latest snapshot (2026-W41, taken October 9) overlaps the previous one by a few days.
- **Reproducibility.** Every number here can be recomputed by downloading the weekly snapshots listed above and summing each extension's value across weeks.
