---
title: "Making remote data feel local in DuckDB: 19 extensions, 2.2 million downloads"
date: 2026-10-09 00:00:00 -0700
description: "Nineteen DuckDB community extensions for caching, storage, accessibility, IO resilience and observability: what each one does, how they fit together, and how many people use them."
tags: [DuckDB, Extensions, Storage, Caching]
---

DuckDB is fast on a laptop SSD. Point it at S3 and you're dealing with round trips, tail latency, throttling and egress bills instead.

Since March 2025, my collaborators and I have shipped 19 DuckDB community extensions to close that gap. They've been downloaded more than 2.2 million times. The most popular one, `cache_httpfs`, is the fifth most downloaded of the roughly 370 extensions in the community catalog.

**TL;DR**

- **Small extensions that compose.** Each one fixes a single problem with remote data, and the filesystem-level ones wrap any DuckDB filesystem, so you only add the layers you need.
- **Five focus areas:** caching, storage backends, accessibility, IO resilience and observability.
- **Six highlights:** `cache_httpfs`, `query_condition_cache` (up to 314× faster on repeated log queries), `cache_prewarm`, `lance_conversion`, `duckherder` and `duckdb_opendalfs`.

![19 DuckDB community extensions grouped into five focus areas](/assets/images/duckdb-extensions/extension-map.png)

*All 19 extensions, grouped by focus area. ★ marks the six highlighted below. Download counts are explained in [Methodology](#methodology).*

---

## Why remote data needs more than `httpfs`

DuckDB's `httpfs` extension does one job well: it lets DuckDB read `s3://` and `https://` URLs. Production workloads quickly need more than that:

- **The same bytes get downloaded again and again.** A dashboard that refreshes every 30 seconds keeps asking for the same Parquet footers and row groups.
- **One slow request can stall a whole scan.** With thousands of `GET`s per query, the slowest one sets the pace.
- **Failures and throttling are normal.** Object stores return 503s and rate-limit clients, and the default timeouts rarely fit your workload.
- **Not everything lives on S3.** Teams also keep data on GCS, Azure Blob, Hugging Face, WebDAV and SFTP.
- **You can't tune what you can't see.** "The query is slow" isn't actionable until you know whether the time went to opens, globs or reads.

Rather than one large extension, I build small ones that each do one thing. The filesystem-level ones expose a `*_wrap` function that takes any registered DuckDB filesystem, including ones I didn't write. For example, here is how to cache a GCS bucket on local disk:

```sql
LOAD duckdb_opendalfs;   -- reach GCS, Azure, Hugging Face, SFTP, ...
LOAD cache_httpfs;       -- cache blocks, metadata and globs on local disk
CALL cache_httpfs_wrap_cache_filesystem('duckdb_opendalfs');
```

`hedged_request_fs`, `rate_limit_fs` and `observefs` plug in the same way.

![The extensions arranged as a layered IO stack](/assets/images/duckdb-extensions/storage-stack.png)

---

## Highlights

### `cache_httpfs`: a persistent read cache for remote files

**The problem.** DuckDB's built-in external file cache keeps data in memory only, for the lifetime of the process. Every restart, new container or notebook kernel downloads the same bytes from object storage again, and pays for the egress again.

**What it does.** `cache_httpfs` is a drop-in replacement for `httpfs` that caches four kinds of things: **data blocks, file metadata, file handles and glob results**. Blocks go to local disk by default (or memory if you prefer), with LRU or access-time eviction, multiple cache directories and a reserved-space limit. Large reads are split into aligned blocks and fetched in parallel. A built-in profiler reports cache hit rates and per-operation latency.

```sql
INSTALL cache_httpfs FROM community;
LOAD cache_httpfs;   -- loads httpfs for you; on-disk cache is the default

SELECT count(*) FROM 's3://my-bucket/events/*.parquet';   -- cold: fetched from S3
SELECT count(*) FROM 's3://my-bucket/events/*.parquet';   -- warm: read from local disk

SELECT * FROM cache_httpfs_cache_access_info_query();     -- hits and misses
```

If anything goes wrong, `SET cache_httpfs_type = 'noop'` turns caching off and you're back to plain `httpfs`.

**Traction.** 1.6 million downloads, about 59,000 last week. It has been in the community catalog's top 10 every archived week since March 2026, and twice reached #2.

<!-- TODO(hao): add a concrete speedup from the cache_httpfs sequential/random read benchmark, ideally with the bar chart. -->

### `query_condition_cache`: a predicate cache for DuckDB

**The problem.** Monitoring dashboards and log investigations run the same `WHERE` clauses over the same tables all day. DuckDB's zone maps can skip a row group only when its min/max statistics rule it out, which rarely happens for `LIKE` patterns or unsorted columns. Everything else gets scanned and filtered again on every run.

**What it does.** It remembers **which vectors in each row group matched a predicate**. On later queries it adds a `ROW_ID` filter, so DuckDB skips the vectors already known to have no matches. Row groups without a cache entry are scanned as usual. The design follows the SIGMOD '24 paper [*Predicate Caching*](https://dl.acm.org/doi/10.1145/3626246.3653395) and [ClickHouse's query condition cache](https://clickhouse.com/blog/introducing-the-clickhouse-query-condition-cache).

```sql
INSTALL query_condition_cache FROM community;
LOAD query_condition_cache;

-- Automatic: built on the first run, used on later runs.
SELECT count(*) FROM logs WHERE level = 'ERROR' AND msg LIKE '%timeout%';

-- Or build an entry ahead of time.
SELECT * FROM condition_cache_build('logs', 'level = ''ERROR''');
```

**Result.** On the [HDFS_v2 log benchmark](https://github.com/logpai/loghub/tree/master/HDFS) (about 71 million log lines), repeated queries ran **up to 314× faster**. The biggest gains came from selective investigation queries.

<!-- TODO(hao): embed docs/img/hdfs_log_bench.png from the repo. -->

### `cache_prewarm`: `pg_prewarm` for DuckDB

**The problem.** The first query after a restart is slow, and on a fresh container or a newly scaled node, a real user is the one who waits.

**What it does.** Modeled on PostgreSQL's `pg_prewarm`, it loads a table's blocks before the first query arrives. It has three modes: `buffer` loads blocks into DuckDB's buffer pool, `read` warms the OS page cache, and `prefetch` sends OS readahead hints. With `cache_httpfs` loaded, it can also pull remote files, including whole glob patterns, into the local disk cache.

```sql
SELECT prewarm('events');                              -- buffer pool
SELECT prewarm('events', 'read', '4GB');               -- OS page cache, capped at 4 GB
SELECT prewarm_remote('s3://lake/events/*.parquet');   -- needs cache_httpfs
```

### `lance_conversion`: `COPY` any query to Lance

**The problem.** [Lance](https://lancedb.github.io/lance/) is a columnar format built for AI and multimodal data. Getting data into it usually takes three steps: export to Parquet, run a separate Python job, then build indexes.

**What it does.** It makes Lance a native `COPY` target. DuckDB reads, filters, joins and casts the data, then streams the result straight into the Rust Lance writer, with no intermediate files. Indexes are declared in the same statement:

```sql
COPY (
  SELECT id, category, description, embedding, asset_uri
  FROM read_parquet('s3://lake/catalog/**/*.parquet')
) TO 's3://lake/catalog.lance' (
  FORMAT LANCE,
  PRESERVE_ORDER false,                  -- one writer per DuckDB thread
  BLOB_COLUMNS (asset_uri),
  SCALAR_INDEX_COLUMNS (id, category),
  VECTOR_INDEX_COLUMNS (embedding),
  TEXT_INDEX_COLUMNS (description)
);
```

It also supports append and overwrite modes and random sampling, and it ships `read_huggingface` and `read_warc` readers. That means going from a Common Crawl WARC file to an indexed Lance dataset takes a single statement.

**Traction.** Released two weeks ago, already about 800 downloads a week.

### `duckherder`: remote and distributed execution over Arrow Flight

**The problem.** DuckDB runs in-process by design. Sometimes the data, or the machine you want to compute on, is somewhere else.

**What it does.** `duckherder` is a DuckDB storage extension. You `ATTACH` a remote server and keep writing the same SQL. Queries run on the server, and results stream back over [Arrow Flight](https://arrow.apache.org/docs/format/Flight.html). On the server side, a driver node splits single-table scans, filters and aggregations across worker nodes. Joins, sorts and writes currently run on the driver alone.

```sql
INSTALL duckherder FROM community;
LOAD duckherder;
SELECT duckherder_start_local_server(8815);
ATTACH DATABASE 'localhost:8815' AS dh (TYPE duckherder);

CREATE TABLE dh.events (id INTEGER, category VARCHAR);
INSERT INTO dh.events VALUES (1, 'click'), (2, 'view'), (3, 'click');
SELECT category, count(*) FROM dh.events GROUP BY ALL;
```

It's experimental, a personal project, and not affiliated with DuckDB Labs. It's also the most ambitious item on this list. The goal is a single writer and many readers sharing one database on object storage, which is what `duckdb_object_storage` is for.

### `duckdb_opendalfs`: one extension, many storage backends

**The problem.** Without it, every storage service needs its own DuckDB filesystem, each with its own URL rules and credential handling.

**What it does.** It connects [Apache OpenDAL](https://opendal.apache.org/) to DuckDB's filesystem layer. One extension covers **S3-compatible stores, GCS, Azure Blob, Hugging Face, WebDAV, SFTP and more**, all configured through ordinary DuckDB secrets (`opendal_s3`, `opendal_gcs`, `opendal_hf`, …). Every operation has a timeout and retries with exponential backoff by default.

```sql
INSTALL duckdb_opendalfs FROM community;
LOAD duckdb_opendalfs;

CREATE SECRET prod_gcs (TYPE opendal_gcs, SCOPE 'gcs://analytics', TOKEN '...');
SELECT * FROM read_parquet('gcs://analytics/events.parquet');
```

Combined with `cache_httpfs`, as in the example near the top of this post, OpenDAL provides access to the backends and `cache_httpfs` provides the speed.

---

## The rest of the stack

**Storage and filesystems**

- **`curl_httpfs`** rebuilds `httpfs`'s HTTP layer on libcurl, with HTTP/2, connection pooling and asynchronous IO. It's fully compatible with `httpfs`. With 270,000 downloads, it's the second most popular extension on this list.
- **`duckdb_object_storage`** stores writable `.duckdb` databases, including the write-ahead log, on local or S3-compatible storage through [SlateDB](https://slatedb.io/).
- **`compression_fs`** adds LZ4, Snappy, Brotli, Bzip2 and XZ support, so `read_csv('logs.csv.xz')` just works.

**IO resilience.** All four wrap an existing filesystem; your queries stay the same.

- **`hedged_request_fs`** sends a backup request when a metadata or listing call is slow, then uses whichever response comes back first. This is the technique from [*The Tail at Scale*](https://research.google/pubs/the-tail-at-scale/).
- **`httpfs_timeout_retry`** sets separate timeouts and retries for open, read, write, list and stat.
- **`rate_limit_fs`** applies rate and burst limits per filesystem and per operation, so you stay under your object store's throttling limits.
- **`latency_injection_fs`** adds realistic, randomized latency to any filesystem. Together with `rate_limit_fs`, it simulates a slow, throttled object store on your laptop.

**Observability and governance**

- **`observefs`** records latency histograms for IO operations, per bucket, and shows how DuckDB's external file cache is being used.
- **`table_inspector`** shows storage details per database, table and column, which helps answer "why is this file 40 GB?"
- **`system_stats`** exposes CPU, memory and disk statistics as tables.
- **`query_limiter`** rejects a query before it runs if DuckDB estimates it would scan more rows than a budget you set. It's a guardrail for shared DuckDB deployments.

**Accessibility**

- **`huggingface`** discovers, scans, profiles and sizes Hugging Face datasets, with `cache_httpfs` underneath.
- **`slack`** lets you search Slack messages with SQL.

---

## By the numbers

![Per-extension cumulative downloads, log scale](/assets/images/duckdb-extensions/downloads-by-extension.png)

`cache_httpfs` accounts for most of the downloads. Remote reads are the first problem most people hit, so that's not surprising. The long tail matters too: the eight IO resilience and observability extensions add up to more than 260,000 downloads.

![Weekly downloads stacked by focus area](/assets/images/duckdb-extensions/weekly-downloads.png)

*Grey bands are weeks the upstream stats archive skipped. They are missing data, not zero downloads.*

<details markdown="1">
<summary>Full table</summary>

| Extension | Focus area | First release | Downloads | Last week |
| --- | --- | --- | ---: | ---: |
| `cache_httpfs` | Cache | 2025-03 | 1,598,643 | 58,798 |
| `curl_httpfs` | Storage | 2025-09 | 270,239 | 3,170 |
| `observefs` | Observability | 2025-09 | 76,867 | 4,666 |
| `system_stats` | Observability | 2025-12 | 36,705 | 931 |
| `httpfs_timeout_retry` | Resilience | 2026-02 | 29,850 | 1,200 |
| `table_inspector` | Observability | 2026-02 | 28,452 | 1,300 |
| `cache_prewarm` | Cache | 2026-02 | 27,240 | 996 |
| `rate_limit_fs` | Resilience | 2026-02 | 26,884 | 951 |
| `hedged_request_fs` | Resilience | 2026-02 | 26,797 | 1,010 |
| `latency_injection_fs` | Resilience | 2026-03 | 25,527 | 953 |
| `query_condition_cache` | Cache | 2026-04 | 18,626 | 908 |
| `duckherder` | Accessibility | 2025-11 | 10,579 | 848 |
| `query_limiter` | Observability | 2026-07 | 9,439 | 866 |
| `duckdb_opendalfs` | Storage | 2026-07 | 8,764 | 1,011 |
| `huggingface` | Accessibility | 2026-08 | 4,884 | 837 |
| `lance_conversion` | Accessibility | 2026-09 | 1,832 | 790 |
| `duckdb_object_storage` | Storage | 2026-09 | 345 | 265 |
| `compression_fs` | Storage | 2026-09 | — | — |
| `slack` | Accessibility | 2026-02 | — | — |
| **Total** | | | **2,201,673** | |

</details>

---

## Thanks

None of this was a solo effort. Thank you to [@peterxcli](https://github.com/peterxcli) (`cache_prewarm`, `observefs`, `query_condition_cache`), [@DouEnergy](https://github.com/DouEnergy) (`cache_httpfs`) and [@Andrewtangtang](https://github.com/Andrewtangtang) (`query_condition_cache`, `table_inspector`). Thanks also to the DuckDB Labs team and the `community-extensions` maintainers, who review these extensions and build them for every platform on every release.

## Try one, then tell me what hurts

Every extension installs the same way:

```sql
INSTALL cache_httpfs FROM community;
LOAD cache_httpfs;
```

Where to start:

- **Dashboards on S3:** `cache_httpfs`
- **Log analytics:** `query_condition_cache`
- **AI datasets:** `lance_conversion`

If you run DuckDB against object storage and something still hurts, I'd like to hear about it. That's usually how the next extension starts. Email me at [dentinyhao@gmail.com](mailto:dentinyhao@gmail.com), or open an issue on [GitHub](https://github.com/dentiny).

<!-- TODO(hao): one-line "about me" (current role, prior work). -->

---

## Methodology

- **Extensions.** Every `description.yml` in [`duckdb/community-extensions`](https://github.com/duckdb/community-extensions) that lists `dentiny` as a maintainer, 19 in total. "First release" is when the descriptor was added to that repository.
- **Downloads.** Weekly snapshots from `community-extensions.duckdb.org/download-stats-weekly/<year>/<week>.json`. There are 76 archived weeks, from 2024-W40 to 2026-W41, ending October 9, 2026.
- **Gaps.** The archive is missing 2025-W22–W37, 2025-W41–W45 and 2026-W01–W09. The 2,201,673 total counts only archived weeks, so it's a lower bound. Interpolating the missing weeks gives about 2.6 million. Extensions released during a gap are undercounted further.
- **Ranking.** Computed from the same snapshots for every community extension. `cache_httpfs` is fifth of 359 in the latest week and fifth of 372 summed over all archived weeks.
- **Caveats.** A download is one `INSTALL`, not one user, so CI pipelines and short-lived containers inflate the counts for every extension. The latest snapshot overlaps the previous one by a few days.
