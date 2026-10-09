---
title: "My DuckDB extensions: caching, storage, and everything around remote data"
date: 2026-10-09 00:00:00 -0700
description: "Eighteen DuckDB community extensions for caching, storage, accessibility, IO resilience and observability: what each one does, how they fit together, and where I'd love help."
tags: [DuckDB, Extensions, Storage, Caching]
---

DuckDB is fast on a laptop SSD. Point it at S3 and you're dealing with round trips, tail latency, throttling and egress bills instead.

Since March 2025, my collaborators and I have shipped 18 DuckDB community extensions to close that gap. They've been downloaded more than 2.2 million times, and `cache_httpfs` is the fifth most downloaded of the roughly 370 extensions in the community catalog.

**TL;DR**

- **Small extensions that compose.** Each one fixes a single problem, so you load only what you need.
- **Six highlights:**
  - [`cache_httpfs`](#cache-httpfs): a persistent cache that reads S3 data up to 345× faster
  - [`query_condition_cache`](#query-condition-cache): a predicate cache that runs repeated queries up to 314× faster
  - [`cache_prewarm`](#cache-prewarm): warms data before the first query
  - [`lance_conversion`](#lance-conversion): turns any query into an indexed Lance dataset in one statement
  - [`duckherder`](#duckherder): runs your SQL on remote servers
  - [`duckdb_opendalfs`](#duckdb-opendalfs): one extension for many storage backends
- **I'm looking for feature requests and collaborations.** See [open problems](#lets-build-together), or email me at [dentinyhao@gmail.com](mailto:dentinyhao@gmail.com).

![18 DuckDB community extensions grouped into five focus areas](/assets/images/duckdb-extensions/extension-map.png)

---

## What hurts, and what fixes it

![Pain points of remote data mapped to the extensions that fix them](/assets/images/duckdb-extensions/pain-to-fix.png)

The filesystem extensions wrap any registered DuckDB filesystem, including ones I didn't write, so they stack. Here is a GCS bucket cached on local disk:

```sql
LOAD duckdb_opendalfs;   -- reach GCS, Azure, Hugging Face, SFTP, ...
LOAD cache_httpfs;       -- cache blocks, metadata and globs on local disk
CALL cache_httpfs_wrap_cache_filesystem('duckdb_opendalfs');
```

![The extensions arranged as a layered IO stack](/assets/images/duckdb-extensions/storage-stack.png)

---

## Highlights

### [`cache_httpfs`](https://github.com/dentiny/duck-read-cache-fs): a persistent read cache for remote files {#cache-httpfs}

DuckDB's built-in file cache lives in memory and disappears when the process exits. `cache_httpfs` is a drop-in replacement for `httpfs` that keeps **data blocks, metadata, file handles and glob results** on local disk, and fetches large reads in parallel.

![cache_httpfs benchmark: 10,681 ms with httpfs, 3,934 ms on first read, 31 ms cached](/assets/images/duckdb-extensions/cache-httpfs-benchmark.png)

```sql
INSTALL cache_httpfs FROM community;
LOAD cache_httpfs;   -- loads httpfs for you; on-disk cache is the default

SELECT count(*) FROM 's3://my-bucket/events/*.parquet';   -- cold: fetched from S3
SELECT count(*) FROM 's3://my-bucket/events/*.parquet';   -- warm: read from local disk
```

Tiny random reads can be slightly slower because reads are aligned to cache blocks; lower `cache_httpfs_cache_block_size` if that's your workload. `SET cache_httpfs_type = 'noop'` turns caching off entirely.

**1.6 million downloads.** It has been in the community top 10 every archived week since March 2026, and twice reached #2.

### [`query_condition_cache`](https://github.com/dentiny/duckdb-query-condition-cache): a predicate cache {#query-condition-cache}

Dashboards and log investigations run the same `WHERE` clauses all day. Zone maps rarely help with `LIKE` patterns or unsorted columns, so DuckDB scans again on every run. This extension remembers **which vectors matched a predicate** and skips the rest next time. The approach follows the SIGMOD '24 paper [*Predicate Caching*](https://dl.acm.org/doi/10.1145/3626246.3653395) and [ClickHouse's query condition cache](https://clickhouse.com/blog/introducing-the-clickhouse-query-condition-cache).

```sql
INSTALL query_condition_cache FROM community;
LOAD query_condition_cache;

-- Built on the first run, used on later runs.
SELECT count(*) FROM logs WHERE level = 'ERROR' AND msg LIKE '%timeout%';
```

![HDFS log analytics benchmark, baseline vs cached, across three investigation stories](/assets/images/duckdb-extensions/qcc-hdfs-benchmark.png)

On the [HDFS_v2 log benchmark](https://github.com/logpai/loghub/tree/master/HDFS) (71 million lines), selective drill-down queries ran **314× and 124× faster**. Queries that match most of the table gain about 1.2×, because there's little to skip.

### [`cache_prewarm`](https://github.com/dentiny/duckdb-cache-prewarm): `pg_prewarm` for DuckDB {#cache-prewarm}

The first query after a restart shouldn't be the slow one. Modeled on PostgreSQL's `pg_prewarm`, this loads data before users arrive: into DuckDB's buffer pool, into the OS page cache, or, with `cache_httpfs`, from S3 to local disk.

```sql
SELECT prewarm('events');                              -- buffer pool
SELECT prewarm('events', 'read', '4GB');               -- OS page cache, capped at 4 GB
SELECT prewarm_remote('s3://lake/events/*.parquet');   -- remote files, via cache_httpfs
```

### [`lance_conversion`](https://github.com/dentiny/duckdb_lance_conversion): `COPY` any query to Lance {#lance-conversion}

![Before: four steps to get data into Lance. With lance_conversion: one COPY statement](/assets/images/duckdb-extensions/lance-pipeline.png)

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

It also supports append, overwrite and random sampling, and it ships `read_huggingface` and `read_warc` readers. Released two weeks ago, it already gets about 800 downloads a week.

### [`duckherder`](https://github.com/dentiny/duckdb-distributed-execution): remote and distributed execution {#duckherder}

![duckherder architecture: client DuckDB, driver, workers, Arrow Flight](/assets/images/duckdb-extensions/duckherder-architecture.png)

```sql
INSTALL duckherder FROM community;
LOAD duckherder;
SELECT duckherder_start_local_server(8815);
ATTACH DATABASE 'localhost:8815' AS dh (TYPE duckherder);

CREATE TABLE dh.events (id INTEGER, category VARCHAR);
INSERT INTO dh.events VALUES (1, 'click'), (2, 'view'), (3, 'click');
SELECT category, count(*) FROM dh.events GROUP BY ALL;
```

It's experimental, a personal project, and not affiliated with DuckDB Labs. It's also the most ambitious item on this list.

### [`duckdb_opendalfs`](https://github.com/dentiny/duckdb-opendal-filesystem): one extension, many backends {#duckdb-opendalfs}

Built on [Apache OpenDAL](https://opendal.apache.org/), it covers **S3-compatible stores, GCS, Azure Blob, Hugging Face, WebDAV, SFTP and more** through ordinary DuckDB secrets, with timeouts and retries on by default.

```sql
INSTALL duckdb_opendalfs FROM community;
LOAD duckdb_opendalfs;

CREATE SECRET prod_gcs (TYPE opendal_gcs, SCOPE 'gcs://analytics', TOKEN '...');
SELECT * FROM read_parquet('gcs://analytics/events.parquet');
```

The other twelve extensions each get a one-line summary on the map at the top of this post.

<details markdown="1">
<summary>All 18 repositories</summary>

| Extension | Repository |
| --- | --- |
| `cache_httpfs` | [dentiny/duck-read-cache-fs](https://github.com/dentiny/duck-read-cache-fs) |
| `query_condition_cache` | [dentiny/duckdb-query-condition-cache](https://github.com/dentiny/duckdb-query-condition-cache) |
| `cache_prewarm` | [dentiny/duckdb-cache-prewarm](https://github.com/dentiny/duckdb-cache-prewarm) |
| `lance_conversion` | [dentiny/duckdb_lance_conversion](https://github.com/dentiny/duckdb_lance_conversion) |
| `duckherder` | [dentiny/duckdb-distributed-execution](https://github.com/dentiny/duckdb-distributed-execution) |
| `duckdb_opendalfs` | [dentiny/duckdb-opendal-filesystem](https://github.com/dentiny/duckdb-opendal-filesystem) |
| `curl_httpfs` | [dentiny/duckdb-curl-filesystem](https://github.com/dentiny/duckdb-curl-filesystem) |
| `duckdb_object_storage` | [dentiny/duckdb-object-storage](https://github.com/dentiny/duckdb-object-storage) |
| `compression_fs` | [dentiny/duckdb-compression-filesystem](https://github.com/dentiny/duckdb-compression-filesystem) |
| `hedged_request_fs` | [dentiny/duckdb-hedged-request](https://github.com/dentiny/duckdb-hedged-request) |
| `httpfs_timeout_retry` | [dentiny/duckdb-httpfs-timeout-retry](https://github.com/dentiny/duckdb-httpfs-timeout-retry) |
| `rate_limit_fs` | [dentiny/duckdb-rate-limit-filesystem](https://github.com/dentiny/duckdb-rate-limit-filesystem) |
| `latency_injection_fs` | [dentiny/duckdb-filesystem-latency-injection](https://github.com/dentiny/duckdb-filesystem-latency-injection) |
| `observefs` | [dentiny/duckdb-filesystem-observability](https://github.com/dentiny/duckdb-filesystem-observability) |
| `table_inspector` | [dentiny/duckdb-table-inspector](https://github.com/dentiny/duckdb-table-inspector) |
| `system_stats` | [dentiny/system_stats](https://github.com/dentiny/system_stats) |
| `query_limiter` | [dentiny/duckdb-query-limiter](https://github.com/dentiny/duckdb-query-limiter) |
| `huggingface` | [dentiny/duckdb-huggingface](https://github.com/dentiny/duckdb-huggingface) |

</details>

---

## By the numbers

![Per-extension cumulative downloads, log scale](/assets/images/duckdb-extensions/downloads-by-extension.png)

![Weekly downloads stacked by focus area](/assets/images/duckdb-extensions/weekly-downloads.png)

<details markdown="1">
<summary>Full table</summary>

| Extension | Focus area | First release | Downloads |
| --- | --- | --- | ---: |
| `cache_httpfs` | Cache | 2025-03 | 1,598,643 |
| `curl_httpfs` | Storage | 2025-09 | 270,239 |
| `observefs` | Observability | 2025-09 | 76,867 |
| `system_stats` | Observability | 2025-12 | 36,705 |
| `httpfs_timeout_retry` | Resilience | 2026-02 | 29,850 |
| `table_inspector` | Observability | 2026-02 | 28,452 |
| `cache_prewarm` | Cache | 2026-02 | 27,240 |
| `rate_limit_fs` | Resilience | 2026-02 | 26,884 |
| `hedged_request_fs` | Resilience | 2026-02 | 26,797 |
| `latency_injection_fs` | Resilience | 2026-03 | 25,527 |
| `query_condition_cache` | Cache | 2026-04 | 18,626 |
| `duckherder` | Accessibility | 2025-11 | 10,579 |
| `query_limiter` | Observability | 2026-07 | 9,439 |
| `duckdb_opendalfs` | Storage | 2026-07 | 8,764 |
| `huggingface` | Accessibility | 2026-08 | 4,884 |
| `lance_conversion` | Accessibility | 2026-09 | 1,832 |
| `duckdb_object_storage` | Storage | 2026-09 | 345 |
| `compression_fs` | Storage | 2026-09 | — |
| **Total** | | | **2,201,673** |

</details>

---

## Let's build together {#lets-build-together}

![Open problems from each project's roadmap, with contact details](/assets/images/duckdb-extensions/help-wanted.png)

Most of these extensions started as someone's problem: a slow dashboard, a flaky bucket, a dataset that needed to be in Lance. If you have one, I want to hear about it.

- **Feature requests and bugs:** open an issue in the extension's repository (listed above).
- **Collaboration:** whether it's one of the open problems above, a new extension, or running these in production at your company, email me at [dentinyhao@gmail.com](mailto:dentinyhao@gmail.com).
- **Just using them?** Tell me what works and what doesn't. That's usually how the next extension starts.

<!-- TODO(hao): one-line "about me" (current role, prior work). -->

## Thanks

Thank you to [@peterxcli](https://github.com/peterxcli) (`cache_prewarm`, `observefs`, `query_condition_cache`), [@DouEnergy](https://github.com/DouEnergy) (`cache_httpfs`) and [@Andrewtangtang](https://github.com/Andrewtangtang) (`query_condition_cache`, `table_inspector`). Thanks also to the DuckDB Labs team and the `community-extensions` maintainers, who review these extensions and build them for every platform on every release.
