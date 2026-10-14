---
title: "Duckherder: Distributed DuckDB With Nothing but a Bucket"
date: 2026-10-14 00:00:00 -0700
description: "Duckherder is a DuckDB extension that spreads queries across workers. Every piece is a DuckDB extension, and the only shared state is one bucket: how writes, reads and Joins work, how we benchmark it, and what comes next."
tags: [DuckDB, Distributed Systems, Object Storage]
---

**[Duckherder](https://github.com/dentiny/duckdb-distributed-execution) is a DuckDB extension that runs your SQL across a driver and a pool of workers. Every piece is a DuckDB extension, and the only shared state is one bucket. Workers keep no data, so adding one means starting another process.**

<!--more-->

![A writer client and reader clients talk to a driver over Arrow Flight. The driver holds the only read-write attachment and sends serialized plans to stateless read-only workers. Everything is stored in one bucket through duckdb_object_storage, with caches at the storage and query layers.](/assets/images/duckherder/architecture.svg)

*The driver is the only writer. Workers are read-only, keep no state, and read the same bucket.*

**In brief**

- **Nothing to deploy but DuckDB.** The client, driver and workers are DuckDB processes with extensions loaded. There's no Spark, Ray or JVM, and no catalog service or metastore database.
- **Storage and compute are separate.** The data is a native DuckDB database in object storage, written through [`duckdb_object_storage`](/posts/duckdb-on-object-storage/). Workers attach it read-only, so nothing gets copied, sharded or rebalanced.
- **One writer, many readers.** Writes go to the driver's single read-write attachment, and DuckDB's own storage engine writes them to the bucket. Reads from read-only attachments are split across workers.
- **The driver ships DuckDB plans, not SQL.** An optimizer extension cuts fragments out of DuckDB's logical plan, splits them by row group, and merges partial results exactly. Small queries never fan out.
- **Joins are distributed without a shuffle.** The driver either splits the biggest table and lets every worker read the small ones, or co-partitions tables that are ordered on the Join key. A cost model decides whether splitting pays off.
- **It's honest about speed.** On TPC-H SF10 with simulated S3 latency, single-table queries ran 1.4× faster with three workers. Those numbers predate Join distribution; a rerun is next.

It's a personal project, experimental, and not affiliated with DuckDB Labs.

## The question I couldn't stop asking

About a year ago I started asking myself what the smallest useful distributed DuckDB would look like. Not a DuckDB-flavored Spark, but DuckDB itself, spread over a few machines, still behaving like DuckDB.

The usual answers come with a stack. Distributed SQL engines bring their own cluster manager, metastore and file format. Putting DuckDB inside one of them means running that framework next to it. I wanted the opposite, so I set two design goals and kept to them:

1. **No extra dependencies to run.** Everything is a DuckDB extension: `INSTALL`, `LOAD`, done. If a piece needs its own service, it doesn't belong.
2. **Storage and compute separation.** The data lives in object storage. Compute is disposable: a worker that crashes, or that I start five minutes ago, is as good as any other, because it holds nothing.

Working back from those goals, four things were missing:

| Piece | What it does | What we use |
| --- | --- | --- |
| Communication | Moves requests, plans and results between processes | [Arrow Flight](https://arrow.apache.org/docs/format/Flight.html) over gRPC, with protobuf messages |
| Shared storage | One copy of the data that every process can read | [`duckdb_object_storage`](https://github.com/dentiny/duckdb-object-storage): a native DuckDB database on S3 |
| Caching | Keeps object-storage latency off repeated queries | [`cache_httpfs`](https://github.com/dentiny/duck-read-cache-fs) at the storage layer, [`query_condition_cache`](https://github.com/dentiny/duckdb-query-condition-cache) at the query layer |
| Query execution | Decides what runs where and merges the results | An optimizer extension on the driver, plain DuckDB on the workers |

Duckherder's job is to tie the four together. The client side is a DuckDB [storage extension](https://github.com/duckdb/duckdb/pull/6066): `ATTACH ... (TYPE duckherder)` adds a catalog whose tables live on the driver, so the SQL stays exactly the same.

```sql
LOAD duckherder;
LOAD cache_httpfs;  -- registers the S3 secret type
CREATE SECRET s3 (TYPE S3, PROVIDER credential_chain, SCOPE 's3://my-bucket/warehouse');

-- Writer: at most one per database.
ATTACH 'driver:8815/sales' AS dh (TYPE duckherder, DATA_PATH 's3://my-bucket/warehouse', SECRET s3);
CREATE TABLE dh.orders AS FROM read_parquet('s3://my-bucket/raw/orders/*.parquet');

-- Readers, from any machine: these queries are split across the workers.
ATTACH 'driver:8815/sales' AS dh (TYPE duckherder, READ_ONLY, DATA_PATH 's3://my-bucket/warehouse', SECRET s3);
SELECT o_orderstatus, sum(o_totalprice), avg(o_totalprice) FROM dh.orders GROUP BY ALL;
```

The client forwards its S3 secret to the driver, and the driver passes it on to the workers with each task.

### One writer, many readers

Note the two `ATTACH` statements above: they're the most important rule of the system. **Each database has exactly one writer and any number of readers.**

![One writer client sends its statements to the driver, which runs them locally and commits them to the bucket; a second writer is rejected. Any number of reader clients send queries to the same driver, which splits them across workers that read the bucket.](/assets/images/duckherder/one-writer.svg)

- **The writer** attaches without `READ_ONLY`. All of its statements, reads included, run on the driver, inside its own transaction. The driver rejects a second writable client for the same database.
- **Readers** attach with `READ_ONLY`, from as many machines as you like. Only their queries are split across workers.
- **Readers see commits.** Workers follow the bucket's committed state and never see a half-written transaction. They can lag a recent commit, by default by about 10 seconds; the roadmap covers how to close that gap.

This rule is what keeps the rest simple. With one writer there's nothing to coordinate between nodes: no distributed locks, no two-phase commit, no conflicting writes to merge. Everything else in Duckherder is about spreading the reads.

Here's one read query from start to finish. The rest of the post zooms into each step.

![A sequence diagram with four lifelines: client, driver, workers and bucket. 1: the client sends the whole SELECT as SQL. 2: the driver binds and optimizes it, cuts a fragment and writes one plan per task. 3: it sends each worker a serialized plan with a rowid range, build ID and storage config. 4: each worker attaches the database read-only on its first task and reads only its task's blocks. 5: workers run the fragment. 6: they return partial results as Arrow batches. 7: the driver merges them, runs the operators above the fragment, and streams the final result to the client.](/assets/images/duckherder/query-lifecycle.svg)

## The write path: let DuckDB write

The write path has almost no distributed logic, and that's on purpose.

![The client plans DML and DDL as one remote statement and sends it over Flight. The driver runs it on its read-write attachment. duckdb_objfs turns WAL appends and 256 KiB blocks into SlateDB keys in the bucket. Read-only workers follow the committed state. Requests carry a transaction ID and sequence number so retries are replayed, not rerun.](/assets/images/duckherder/write-path.svg)

**On the client**, the Duckherder catalog overrides DuckDB's `PlanInsert`, `PlanUpdate`, `PlanDelete` and `PlanMergeInto`. Instead of executing locally, each becomes one operator that sends the statement to the driver and returns its result. DDL works the same way: `CREATE TABLE` runs on the driver first, then lands in the client's in-memory catalog cache. A few details took more care than expected:

- **Prepared statements** exist only in the client's connection, so the client re-prepares them on the driver under a unique name and binds the arguments there, with no string substitution.
- **`RETURNING`** asks the driver for whole rows, and the client evaluates the expressions, so they see exactly the client's bindings.
- **Retries are safe.** Every request carries a transaction ID and a sequence number. If a reply gets lost and the client retries, the driver recognizes the request and returns its saved response instead of running the `INSERT` again.

**On the driver**, the statement runs against the single read-write attachment of a `duckdb_objfs://` database. The driver rejects a second writable client for the same database. From here on it's just DuckDB: constraints, ART indexes, the WAL and checkpoints all behave as on a laptop. Underneath, `duckdb_object_storage` turns the WAL appends and 256 KiB blocks into SlateDB keys in the bucket. I wrote about how that works [in the previous post](/posts/duckdb-on-object-storage/).

**On the workers**, there's nothing to do. A worker attaches the database read-only on its first task and caches the attachment. SlateDB readers only ever see committed states, so a worker can never compute over a half-written checkpoint.

The cost of this design is that writes don't scale out: one database has one writer. For analytics that's usually the right trade. Loads and transformations are bulk operations that DuckDB already parallelizes on one machine, and the expensive part, reading, is what spreads.

## The read path: small stays put, large splits

Reads start on the client. If a query reads only remote tables from one database and the client hasn't changed any setting that affects results, the client sends the whole `SELECT` to the driver. Otherwise, say for a join between a remote table and a local one, it reads each remote table with its projections and filters pushed down, and joins locally. Either way, results come back as Arrow batches that client threads convert in parallel.

![Left: whole-query pushdown, where the client sends the rewritten SQL of a query that reads only remote tables of one database, keeps default settings, calls no client macros or session functions, has no prepared parameters and depends on no time zone. Right: per-table remote scans for everything else, where each remote table is fetched with only the needed columns and pushed-down filters, and the client joins locally.](/assets/images/duckherder/client-pushdown.svg)

The interesting part happens on the driver. An optimizer extension, running after DuckDB's own optimizers, walks down the plan and looks for the first aggregate, Join or table scan it can turn into a **worker fragment**. If it finds one, that subtree is replaced with a table function that runs the fragment on workers, and the operators above it stay on the driver.

![Left: the optimized plan ORDER BY, PROJECTION, AGGREGATE, HASH JOIN over lineitem and orders, with the aggregate and everything below it outlined. Middle: what stays on the driver: ORDER BY, PROJECTION, a projection that finishes avg, a merge aggregate, and the worker_fragment table function. Right: what every worker gets: a projection pinning the column order, a partial aggregate computing sums and a count, the hash join, a scan of lineitem restricted to rowid range i, and a full scan of orders.](/assets/images/duckherder/fragment-cut.svg)

![Left: a small query is shipped to the driver, which runs it itself or as a single task. Right: a large table's row groups are assigned to three workers, each computes partial sums and counts over its own row groups, and the driver merges them.](/assets/images/duckherder/read-path.svg)

### Small data never fans out

Distributing a query has a fixed cost: serializing plans, a network round trip per task, converting results. For small data that cost is the whole query, so the driver keeps it local whenever splitting can't help:

![A flowchart of the questions the driver asks: is it a read-only client in autocommit with workers and a database in a bucket; is there an aggregate, Join or scan before any LIMIT; can workers read every table under it; does splitting keep the result the same; does it split into two or more tasks; and for a Join, is one task faster than one node with hash tables that fit in memory. Any no keeps the work on the driver, except that a single-table fragment over one row group runs as one task. All yes fans out one task per worker.](/assets/images/duckherder/fanout-decision.svg)

- **A table with one row group** (up to 122,880 rows) can't be split. A single-table fragment over it is sent as a single task, and a Join in which no input splits into at least two tasks runs on the driver.
- **A query with a `LIMIT`** may need only a few rows, which the driver reads faster than workers can return a whole table.
- **A Join the cost model rejects** stays local. More on that below.
- **The writer's own queries, explicit transactions, and failed fragments** run on the driver too. Fragments run outside the client's transaction, so they would miss its snapshot. If any task fails, the driver reruns the whole fragment locally rather than silently dropping that task's rows.

### Large data splits by row group

DuckDB stores a table as row groups of 122,880 rows, each with its own column segments and min/max statistics. That makes the row group the natural unit of work: whole row groups are read independently, and splitting one would make every task read all of it.

So for a large table, the driver assigns contiguous runs of row groups to tasks, one task per worker, and expresses each run as a filter on DuckDB's `rowid`. The first task's range starts at zero and the last one has no upper bound, so rows appended after planning are still covered exactly once. On the worker, a small optimizer extension replaces the table scan with a range scan that skips straight to the task's first row group and stops after its last. The worker only fetches its own blocks from the bucket.

![A table with 12 row groups split into three tasks by rowid: task 1 reads rowids below 491,520, task 2 reads from 491,520 to below 983,040, and task 3 reads from 983,040 with no upper bound. On worker 2 the table scan becomes a row group range scan over row groups 4 to 7; row groups 0 to 3 and 8 to 11 are never opened, so the bucket only serves the blocks of row groups 4 to 7, and only the columns the plan reads.](/assets/images/duckherder/row-group-scan.svg)

Results are merged exactly:

- `sum`, `min` and `max` are merged by the same function, and `count` by summing the counts.
- `avg` is split into a sum and a count on the workers, and the driver divides them the way DuckDB's `avg` does. That's exact for integers and decimals. For floating point, the sum depends on row order, so it stays local.
- `count(DISTINCT x)` and friends make each task return its distinct values of `x`, and the driver aggregates their union.
- Anything else, like `string_agg`, `FILTER` clauses or ordered aggregates, keeps the aggregate on the driver.

![A table with three columns: the aggregate in the query, what each worker returns, and what the driver computes. sum, min and max are merged by the same function; counts are summed and cast to BIGINT; avg over integers or decimals becomes a sum and a count, divided on the driver the way DuckDB divides; count DISTINCT becomes the distinct values of x, counted again over their union. avg over DOUBLE, string_agg, lists, quantiles, FILTER and ORDER BY clauses, top-N min and max, and grouping sets stay on the driver.](/assets/images/duckherder/aggregate-merge.svg)

### Plans, not SQL

The first version sent SQL to the workers: the driver rewrote each query into per-worker SQL with `rowid BETWEEN` predicates. It worked for simple scans, then got harder with every feature, because a bound and optimized plan doesn't turn back into SQL cleanly. `CASE`, `IN`, correlated subqueries and table bindings each needed their own renderer, and some never round-tripped.

So the driver now sends DuckDB's own serialized logical plans. A worker deserializes the fragment, optimizes it again and runs it, and the SQL path is gone. A few rules keep that safe:

- **Plans carry a build ID.** The serialization format isn't stable across DuckDB builds, so a worker rejects plans from a different build instead of misreading them.
- **Output columns are pinned.** The worker's optimizer may reorder a Join's inputs or merge duplicate aggregates, so every fragment is wrapped in a projection that fixes its column order.
- **No planning-time statistics in the plan.** DuckDB's compressed materialization bakes the driver's statistics into the plan, and workers may see newer data, so it's disabled on the driver.

![The removed first version rendered the bound plan back to SQL with rowid BETWEEN, and the worker parsed, bound and planned it again. Now the driver copies the fragment from the optimized plan, wraps it in a projection that pins the columns, makes one copy per task with that task's range, and serializes it with DuckDB's plan serializer and a build ID. The worker checks the build ID, deserializes, optimizes again, executes, and streams Arrow batches back.](/assets/images/duckherder/plan-shipping.svg)

## Joins without a shuffle

When I wrote about my extensions [last week](/posts/duckdb-extensions/), Joins still ran on the driver. Most distributed engines distribute a Join by shuffling: every worker hash-partitions both inputs by the Join key and sends each row to its owner. That's a lot of machinery: worker-to-worker connections, buffering, back pressure, and failure handling in the middle of a query.

Shared storage gives us two cheaper options first.

![Left: lineitem is split into three row group ranges, and every task reads all of part. Right: lineitem and orders are both ordered on orderkey, so each task reads the same key range from both tables.](/assets/images/duckherder/joins.svg)

### Split the biggest table, replicate the rest

The driver splits the input that reads the most bytes by row group, as above, and every task reads the other inputs in full. It's a broadcast Join where nobody broadcasts: each worker reads the small tables straight from the bucket. It works for any equality key and for Joins of three or more tables.

Not every Join type allows it. For an inner Join, either side can be split. For left, semi and anti Joins, each left row needs to see every right row it could match, so only the left side may be split, and right Joins mirror that. A full outer Join can't be split this way at all.

![A matrix of Join types against three ways to split. Inner Joins allow all three. Left, semi, anti and single Joins allow splitting the left side or both sides by key. Right Joins allow splitting the right side or both by key. Full outer Joins only allow splitting both by key. Mark Joins only allow splitting the left side.](/assets/images/duckherder/join-rules.svg)

### Co-partition by key ranges

The second option uses something tables often already have: an order. Data is usually loaded in roughly key order, by ID or by date, and DuckDB keeps min/max statistics for every row group. When the row groups of both inputs are ordered on the Join key, the driver reads those minimums, cuts the key space into ranges with about the same number of row groups each, and gives each task one range of every table. Rows with equal keys always meet in the same task.

![Step 1: read the minimum orderkey of each of lineitem's 12 row groups, from 1 to 55 million, and check each is above the previous maximum. Step 2: cut at the minimums 20 million and 40 million, so three tasks get four row groups each; the first range also takes NULL keys. Step 3: apply the same ranges to orders, whose six row groups are ordered on the key too. Each worker's zone maps then skip every row group outside its range in both tables.](/assets/images/duckherder/key-range-cuts.svg)

In TPC-H, `lineitem` and `orders` are both ordered on `orderkey`. Co-partitioning them means nothing is read twice, and since equal keys never cross tasks, left, right, semi, anti and even full outer Joins split correctly. The same reasoning extends upward. A `GROUP BY` on the key keeps whole groups within one task, and correlated subqueries, which DuckDB plans as a `DELIM_JOIN`, split when the outer query is split on the correlated column.

Key ranges need statistics that are exact and ordered like the values. Integers, decimals, dates, times and timestamps qualify. Strings don't, because DuckDB truncates their statistics. Floating-point keys have `NaN` and `-0.0`, and `TIME WITH TIME ZONE` statistics aren't ordered by instant.

### Should the Join be split at all?

Replicating isn't free: every task reads the small tables again, and all tasks hit the same storage. So before splitting a Join, the driver compares the estimated time on one node with the time of one task:

- **On one node**, the query scans the big table once, then each other input, and builds a hash table on the smaller side of each Join.
- **In one task**, the query scans its share of the big table, plus every replicated input in full, and builds on the smaller of each replicated input and its share of the big one.

Because tasks share the bucket but not CPUs, each replicated read is charged a contention factor of \((n + 1) / 2\) for \(n\) tasks. The driver splits only when one task finishes first and every task's hash tables fit in memory. Estimates come from DuckDB's own cardinality estimates and the same row widths its build-side optimizer uses.

![Two illustrative Joins on three workers. A: a 30 GB fact table joined with a 1 GB dimension reads 31 GB on one node, but only 12 GB per task (a 10 GB share plus the dimension charged twice), so it splits. B: 30 GB joined with 20 GB that isn't ordered on the key reads 50 GB on one node and also 50 GB per task (a 10 GB share plus 20 GB charged twice), so it stays local.](/assets/images/duckherder/cost-model.svg)

Joins without an aggregate above them are distributed too, but only when the operators above just project or sort. Otherwise every joined row would travel back to the driver, a cost the model doesn't count yet. For experiments, `SET duckherder_join_partitioning = 'always'` (or `'never'`) overrides the cost model.

## Caching at two layers

Object storage is far away: a cold S3 read costs tens of milliseconds before the first byte arrives. Two of my other extensions take that cost off repeated queries, at different layers:

- **At the storage layer, [`cache_httpfs`](https://github.com/dentiny/duck-read-cache-fs)** caches the bytes, metadata and glob results of anything read from S3 through `httpfs`, in memory or on local disk. In Duckherder that covers external data, such as the Parquet files in the load above. The database itself goes through SlateDB, which has its own memory and disk block caches.
- **At the query layer, [`query_condition_cache`](https://github.com/dentiny/duckdb-query-condition-cache)** remembers which row groups and vectors match a filter. The next time the same predicate shows up, DuckDB skips the rest without reading their blocks. Dashboards run the same filters all day, which is exactly this case.

Today each cache is local to the process that loads it, and Duckherder doesn't configure them on workers automatically yet. Making the caches and the task placement work together is on the roadmap.

## How we benchmark

A distributed benchmark is easy to fool yourself with, so the setup is mostly about removing ways to cheat. It lives in [`benchmark/tpch`](https://github.com/dentiny/duckdb-distributed-execution/tree/main/benchmark/tpch), and one script runs it end to end.

- **The workload is TPC-H.** It has 22 queries over a schema with real Joins, and DuckDB generates the data. We load it into a `duckdb_objfs://` database once per scale factor.
- **Storage is local but S3-compatible.** The bucket is [RustFS](https://github.com/rustfs/rustfs) running in Docker, pinned to its own CPU cores.
- **The latency is injected.** Local storage answers in microseconds, which hides exactly the cost we care about. A [latency injection extension](https://github.com/dentiny/duckdb-filesystem-latency-injection) wraps the filesystem that `duckdb_object_storage` registers, in the driver and every worker. Reads sleep a log-normal 30 ms (standard deviation 15 ms) plus 1 ms per 88 KB, stats 20 ms and lists 40 ms, roughly S3 Standard in-region.
- **Every process gets its own CPUs and memory.** The client, driver and each worker run in separate systemd scopes with fixed cores and a memory limit. DuckDB sizes its thread pool from that quota, so no process can steal another's cores.
- **Every run is cold.** With `--cold`, the driver and all workers restart before every single query run, so no run benefits from a cache warmed by an earlier one.
- **Results are checked before they're timed.** Every query's result is compared with plain DuckDB over a local copy of the same data. A fast wrong answer fails the run.
- **Runs record their context.** Each run writes the commit, settings, CPU and memory limits, and startup SQL next to the timings, plus per-query logs and the driver's execution stats showing what was split.

![The CPU map: client on CPUs 0-1, workers on 2-3, 4-5 and 6-7, driver on 8-9, each with 4 GiB, and RustFS on efficiency cores 16-19 with 2 GiB in Docker. In the driver and every worker, a latency injection wrapper sits between DuckDB's storage engine and the SlateDB filesystem. One run loads TPC-H once, starts the cluster for each worker count, checks all 22 results against plain DuckDB, restarts every process before each timed run, and writes medians, timings, logs, stats and the commit and settings used.](/assets/images/duckherder/benchmark-setup.svg)

Here's SF10 on an i7-12700K. Each DuckDB process gets two threads of one performance core and 4 GiB, and RustFS gets the four efficiency cores:

![TPC-H SF10, cold start, with and without simulated S3 latency, 0 workers versus 3 workers. Q1 and Q6 drop from about 29 and 30 seconds to about 20 and 21 seconds with simulated latency; other queries are unchanged.](/assets/images/duckherder/tpch-sf10-cold.png)

These numbers are from before Joins were distributed, so only the two single-table queries, Q1 and Q6, ran on the workers. With simulated latency, they went from 28.8 s to 20.0 s and from 29.9 s to 21.1 s, about 1.4× faster with three workers. Without it, they gained 1.4× and 1.25×. Every other query ran on the driver and stayed within noise. That's the honest starting point: correct results, a real speedup where the work was split, and far from the 3× ideal. Finding where the rest goes is the next job.

Two more things stand out. Simulated latency makes everything about 12× slower, so for a cold workload object storage is the bottleneck, not CPU. That's why caching matters as much as distribution. And now that Joins split, the other 20 queries can finally move. Rerunning this benchmark is the first item below.

## What's next

### Distributing table functions, starting with `read_parquet`

Fragments only split native DuckDB tables today, because the row group is the unit of work. Table functions are the obvious next source of parallelism, and `read_parquet` is the most important one: its files and row groups split as naturally as a table's. With `cache_httpfs` on each worker, a recurring query over a Parquet lake would read each byte from S3 once per worker, not once per query.

### A real shuffle

Replicate-and-split and key ranges cover a lot, but not everything: Joins between two large tables that aren't ordered on the key, `GROUP BY`s with millions of groups, and Joins whose output is too large to return to the driver. Those need workers to exchange data by hash partition, and multi-stage plans where one stage's output feeds the next. The transport is the easy part. The hard parts are memory, back pressure and failures in the middle of a stage.

![Left: distributed read_parquet, where the driver globs the files, reads their footers and assigns Parquet row groups to three workers, each with cache_httpfs. Right: a two-stage shuffle, where three workers scan and hash rows by the Join key and send each row to the worker that owns its hash range, which then runs the Join or GROUP BY.](/assets/images/duckherder/roadmap-shuffle.svg)

### One snapshot per query

Read-only workers follow the writer's SlateDB manifest, by default about every 10 seconds. A worker can therefore lag a recent commit, and two workers in the same query can see different versions. The fix is to pin every query to one committed snapshot, chosen by the driver and sent with each task. With a strong-consistency mode in `duckdb_object_storage`, that becomes "the latest commit, on every worker."

![A timeline with commit 41, commit 42 and then the query start. Today, two workers that polled after commit 42 read it, while a worker that last polled before it reads commit 41. Pinned, the driver picks commit 42 for the query and sends it with every task, so all three workers read commit 42.](/assets/images/duckherder/snapshot-pinning.svg)

### Building on Quack

In May, DuckDB Labs released [Quack](https://duckdb.org/2026/05/12/quack-remote-protocol), DuckDB's own client-server protocol: one DuckDB instance attaches another over HTTP. It's in beta in DuckDB v1.5.3, with a stable release planned for v2.0. Quack didn't exist when I started Duckherder, so we built on Arrow Flight. I'd like to move both links, client to driver and driver to workers, onto Quack:

![Today: Arrow Flight and protobuf carry SQL text or scans with filters rendered back to SQL, results go through Arrow and lose some types, gRPC, Arrow, Flight and protobuf are in every build, there's no TLS or authentication, and whole-query pushdown refuses many queries. With Quack: a remote pushdown operator carries a serialized plan subtree, results keep every type in DuckDB's own serialization, a query takes one round trip over HTTP(S), TLS and authentication come with it, and the dependencies go away. Below, a query joining a local table with remote tables sends the remote aggregate, filter and Join to the driver as one remote pushdown operator and gets back only the aggregated rows.](/assets/images/duckherder/quack.svg)

- **A remote pushdown operator.** Today the client gives the driver either SQL text or a table scan whose filters it renders back to SQL, and only comparisons, `AND`/`OR` and `IS NULL` survive that. Whole-query pushdown refuses anything the server might resolve differently: session functions, time zones, client macros, and queries mixing local and remote tables. A remote pushdown operator would carry a serialized plan subtree instead (filters, aggregates and Joins over remote tables) in DuckDB's own format, the way the driver already ships fragments to workers. A query joining a local table with two remote ones would push the remote Join and aggregate to the driver and get back only the aggregated rows. Quack doesn't carry plans today, but it's built on DuckDB's own serialization, and DuckDB Labs is considering letting extensions add their own protocol messages. That's exactly the hook this needs.
- **No Arrow round trip.** Every hop today converts DuckDB vectors to Arrow and back, and `UNION`, `BIT` and `BIGNUM` values don't survive. Quack sends results in DuckDB's own serialization, fetched by several threads in parallel. In DuckDB Labs' benchmark, it moved bulk results faster than Arrow Flight SQL.
- **One round trip per query**, which matters for the small queries that never fan out.
- **Fewer dependencies.** Quack ships with DuckDB, so Duckherder could drop gRPC, Arrow, Flight and protobuf from its build: the "nothing extra" goal, applied to the build too.
- **Security that comes with it.** Quack runs over HTTP(S) behind any reverse proxy and has pluggable authentication, which covers most of the production basics below.

### Cache-aware scheduling

Today, task \(i\) goes to whichever worker is \(i\)th in the list. If the same row group range always went to the same worker, its SlateDB, `cache_httpfs` and `query_condition_cache` entries would keep hitting, so a warm cluster would behave like a big cache spread over machines. Duckherder should also configure those caches on workers itself.

### Fewer reasons to fall back

- **Retry a failed task on another worker**, instead of rerunning the whole fragment on the driver.
- **Distributed `ORDER BY` and top-N**, with sorted runs merged on the driver.
- **Stream results** instead of materializing every task's output on the driver first.
- **Workers report their memory**, so the cost model stops assuming they match the driver.

### Production basics

TLS and authentication come first, on Flight or by moving to Quack: today the S3 credentials travel to workers unencrypted, so Duckherder belongs on a trusted network only. After that come dynamic worker scaling and per-query resource tracking.

## Try it

```sql
INSTALL duckherder FROM community;
LOAD duckherder;
SELECT duckherder_start_local_server(8815);
SELECT duckherder_start_standalone_worker(8816);
SELECT duckherder_start_standalone_worker(8817);
```

That gives you a driver and two workers in one process, enough to watch `duckherder_get_query_execution_stats()` show how each query was split. For a real cluster, start `distributed_worker` on each machine and point the driver at them. Workers only run on shared storage, so attach with a `DATA_PATH` in a bucket. Without one, the database lives in the driver's memory and every query runs there.

Source, issues and the benchmark are on [GitHub](https://github.com/dentiny/duckdb-distributed-execution). If you have a workload that should be distributed and isn't, I'd love to hear about it: [dentinyhao@gmail.com](mailto:dentinyhao@gmail.com).
