---
title: "A Real DuckDB Database in S3, With Nothing Else to Run"
date: 2026-10-09 00:00:00 -0700
description: "duckdb_object_storage stores native DuckDB databases (indexes, constraints, WAL and all) directly in object storage, with no table format, catalog database or fork of DuckDB."
tags: [DuckDB, Object Storage, SlateDB]
---

**`duckdb_object_storage` stores native DuckDB databases (indexes, constraints, WAL and all) directly in object storage. There's no table format, no catalog database and no fork of DuckDB. You need DuckDB and a bucket.**

<!--more-->

![Iceberg needs a catalog service and DuckLake needs a catalog database. duckdb_object_storage needs only the bucket.](/assets/images/duckdb-object-storage/stack-comparison.svg)

*Every option stores data in object storage. The difference is what else you have to keep running next to it.*

**In brief**

- `duckdb_object_storage` is a DuckDB community extension. `ATTACH 'duckdb_objfs://analytics.db'` gives you an ordinary DuckDB database whose bytes live in S3, any S3-compatible service, a local directory, or memory.
- **The only dependency is object storage.** There's no catalog service, no Postgres, no coordinator and no compactor to run. All metadata, including writer fencing, lives in the bucket.
- **DuckDB's storage engine is untouched.** The extension replaces the disk underneath it, so ART indexes, enforced primary and foreign keys, the WAL, checkpoints and `COPY` all behave as they do on a laptop.
- **Underneath are [SlateDB](https://slatedb.io/) and [Apache OpenDAL](https://opendal.apache.org/).** SlateDB is an LSM tree built for object storage, with each DuckDB block stored as one SlateDB key. OpenDAL gives every storage backend one code path.
- **One writer, any number of readers.** Readers always see a state the writer committed, never a partial write. That makes the extension the storage layer for our distributed execution engine, [Duckherder](https://github.com/dentiny/duckdb-distributed-execution).
- **The numbers.** On TPC-H, reads are within about 10% of native DuckDB on local storage, and cold reads from S3 are about 1.2× slower than native DuckDB over HTTPFS. Writing straight to S3 matches "write locally, then upload."
- **Tested with DuckDB's own test suite.** The skip list for extension bugs is empty.

DuckDB is a database that lives in a file. That's a large part of its charm: no server, no setup, and a whole analytical database in one `.duckdb` file you can copy around.

It's also where things get awkward once more than one machine is involved. The file sits on one disk. If the machine goes away, so does the database, unless someone remembered to copy it. If you want to query it somewhere else, you ship the file there first. If you want to share it, you build it locally, upload it to S3, let readers open it read-only over HTTPFS, and repeat the whole cycle every time the data changes.

The rest of the data stack settled this question years ago: object storage is the durable source of truth, and compute is stateless and disposable. We wanted DuckDB to work that way too, without having to stop being a database.

## What it looks like

```sql
INSTALL duckdb_object_storage FROM community;
LOAD duckdb_object_storage;
LOAD httpfs;  -- provides the S3 secret type

-- Credentials come from DuckDB's Secret Manager, like any other S3 access.
CREATE SECRET (TYPE S3, PROVIDER credential_chain, REGION 'us-east-1', SCOPE 's3://my-bucket');

SET duckdb_objfs_backend = 's3';
SET duckdb_objfs_bucket  = 'my-bucket';

ATTACH 'duckdb_objfs://analytics.db' AS db;
USE db;

CREATE TABLE customers (
    id    BIGINT PRIMARY KEY,
    email VARCHAR UNIQUE NOT NULL
);
CREATE TABLE orders (
    id          BIGINT PRIMARY KEY,
    customer_id BIGINT REFERENCES customers(id),
    amount      DECIMAL(12, 2) CHECK (amount >= 0)
);
CREATE INDEX orders_by_customer ON orders(customer_id);

INSERT INTO customers VALUES (1, 'ada@example.com');
INSERT INTO orders VALUES (10, 1, 42.00);
CHECKPOINT db;

INSERT INTO orders VALUES (11, 99, 1.00);
-- Constraint Error: Violates foreign key constraint because key "id: 99"
-- does not exist in the referenced table
```

None of that SQL knows it's talking to S3, and the constraints are enforced exactly as they are on local disk. On another machine, a second process can attach the same database read-only and query it right away:

```sql
ATTACH 'duckdb_objfs://analytics.db' AS db (READ_ONLY);
SELECT c.email, sum(o.amount) FROM db.orders o JOIN db.customers c ON o.customer_id = c.id GROUP BY ALL;
```

For development, skip the S3 settings: the default backend stores everything under `./.duckdb_objfs` on local disk, using the same code path.

## Why this matters

The extension is useful wherever DuckDB already does the work and the local disk is what's in the way:

- **Ingest and ETL jobs** that build a DuckDB database on ephemeral compute and want it to outlive the container.
- **Read fleets** (dashboards, notebooks, API services) that should all see one database without copying files around.
- **Embedded analytics** in applications that run in more than one place.
- **Teams that want a database, not a lakehouse**: something with keys, indexes and constraints, where the only thing to operate is a bucket.

## A table format is not a database

DuckDB can already read and write data in object storage through Iceberg and DuckLake. Those are good tools, and we use them. But they solve a different problem: they let many engines share tables stored as Parquet files, and they pay for that with a catalog and a narrower feature set. A table format gives you the features the format defines. A database gives you everything the database does.

|  | duckdb_object_storage | DuckLake | Iceberg |
| --- | --- | --- | --- |
| Runs besides the bucket | Nothing | A catalog database* | A catalog service |
| Data format | Native DuckDB | Parquet | Parquet |
| Indexes (ART, HNSW, R-tree) | Yes | No | No |
| Enforced primary, unique and foreign keys | Yes | No | No |
| Small, frequent writes | WAL, folded in at checkpoint | New files per commit, or inlined in the catalog | New files per commit |
| Engines that can read it | DuckDB | DuckDB; open spec | Spark, Trino, Flink, DuckDB, … |
| Concurrent writers | One | Many | Many |

\* A DuckDB or SQLite file works on a single machine. Once several machines share the lake, the catalog goes in PostgreSQL or MySQL.

What you keep by storing the native format:

- **Indexes.** DuckDB stores ART indexes for primary keys, unique constraints and `CREATE INDEX` in the database file. Extensions can do the same: `spatial` keeps its R-trees there, and `vss` can persist HNSW vector indexes (still behind an experimental flag). Parquet has nowhere to put them.
- **Constraints that are actually enforced.** `PRIMARY KEY`, `UNIQUE`, `FOREIGN KEY`, `CHECK` and `NOT NULL` all reject bad rows when they're written, not later when someone runs a query.
- **The whole catalog.** Views, macros, sequences, generated columns, enums, comments and schemas are all stored with the data.
- **The whole type system and DuckDB's own compression.** Every type round-trips without being mapped to Parquet types. Data is compressed with DuckDB's own codecs, such as ALP, FSST, dictionary and RLE.
- **Cheap small writes.** A single-row `UPDATE` goes into the WAL, and the next checkpoint folds it into the database. It doesn't produce a new data file, a delete file, a snapshot, or later a compaction job.
- **Everything around the database.** `COPY ... TO`, `COPY FROM DATABASE`, `EXPORT DATABASE`, `CHECKPOINT`, and every extension that works on an attached database.

And you don't have to choose. One DuckDB session can attach a `duckdb_objfs://` database, an Iceberg catalog and a DuckLake together, and join across all three. Use the native format for data you own and query in DuckDB, and use a table format for tables that other engines need to share.

> The only thing to operate is a bucket.

That last row of the stack diagram is the point we care about most. Every extra service is something to provision, secure, back up, upgrade and page someone about. Here, the bucket is the entire deployment. SlateDB keeps its manifest in the bucket and uses it to coordinate writers, so even writer fencing doesn't need anything outside it.

## Replace the disk, not the database

![The DuckDB storage engine calls a duckdb_objfs filesystem, which calls a Rust core, SlateDB and OpenDAL, which talk to S3, the local filesystem or memory.](/assets/images/duckdb-object-storage/architecture.svg)

The extension registers a DuckDB `FileSystem` for the `duckdb_objfs://` scheme. DuckDB's storage engine opens, reads, writes, truncates, syncs, moves and removes files the same way it always does; it just happens to be talking to us. A thin C++ layer forwards each call through a C FFI to a Rust core, which maps files onto SlateDB. OpenDAL sits at the bottom, so S3, the local filesystem and memory all run the same code.

> We didn't change DuckDB's storage engine. We replaced the disk underneath it.

This is the decision everything else follows from. Since DuckDB's on-disk format is untouched, every feature in the previous section comes along unchanged, and so will storage features DuckDB adds in future releases.

### Why not one object per file?

The obvious design is to store `analytics.db` as one object called `analytics.db`. It falls apart on the first write.

DuckDB updates its files in place: it rewrites 256 KiB blocks at arbitrary offsets, appends to the WAL, and updates its headers. Objects in object storage are immutable. Changing one block of a 20 GB object means writing a new 20 GB object.

What we needed was a storage engine built for this exact mismatch, and SlateDB is one: an embedded LSM tree whose WAL, memtable flushes and compacted SSTs all live in object storage. It gives us batched durable writes without rewriting large objects, atomic write batches, serializable transactions, background compaction that reclaims overwritten blocks, non-fencing readers, and caching on the read path. Compaction and garbage collection run as background tasks inside the writer process, so even they don't need a service of their own.

That's also why the core is written in Rust: SlateDB and OpenDAL are both Rust libraries.

### One storage layer for every backend: OpenDAL

SlateDB talks to storage through an object-store interface, and we give it an adapter over an [Apache OpenDAL](https://opendal.apache.org/) `Operator`. Choosing a backend just means choosing which `Operator` to build: S3 (or MinIO), a local directory, or memory. Everything above that line is identical, so the local backend you develop against runs the same code as production on S3, and the memory backend makes tests fast without mocking anything.

OpenDAL gives us more than portability:

- **Layers.** `duckdb_objfs_io_stats()` is an OpenDAL layer wrapped around every operation, recording request counts, latency and payload bytes, whichever backend is in use.
- **Correct semantics on each backend.** On the local filesystem, OpenDAL writes through a temporary directory and renames into place, so SlateDB never sees a half-written manifest.
- **Room to grow.** We compile in S3, the local filesystem and memory today. OpenDAL supports dozens of other services, including GCS, Azure Blob Storage and HDFS, so a new backend is mostly a feature flag plus configuration plumbing, not a new storage integration.

### Every block is exactly one key

![A DuckDB file's 12 KiB header and 256 KiB blocks each map to one SlateDB key. Paths, metadata and content use three separate key prefixes.](/assets/images/duckdb-object-storage/chunk-layout.svg)

Every DuckDB database gets its own SlateDB instance, and inside it we keep a very small filesystem. A path key maps a file name to a file ID. A metadata key holds the file's size, modification time and chunk layout. The file's content is stored as chunks under `c/<file id>/<chunk index>`. IDs and indexes are fixed-width hex, so a file's chunks sort next to each other, and reading a byte range is one range scan that can't run into another file's keys.

The chunk geometry is the part that matters. Chunks are 256 KiB, the same as DuckDB's block allocation size, and they start after a 12 KiB offset, the size of DuckDB's file header (3 × 4 KiB). The header is chunk 0, and from then on every DuckDB block lands on exactly one chunk. Writing a block is one `put` with no read-modify-write, and reading a block is one `get`. Before we aligned them, every block straddled two chunks, and every block write had to read and rewrite both.

DuckDB's companion files (`.wal`, `.wal.checkpoint` and `.wal.recovery`) live in the same SlateDB instance as their database, which is what makes the next part possible.

### fsync means durable in the bucket

![FileSync puts staged chunk writes, deletes and the metadata update in one write batch and flushes it to the bucket. A checkpoint's rename is one transaction that only re-points a path key.](/assets/images/duckdb-object-storage/write-path.svg)

Writes are staged in memory per file handle. When DuckDB calls `FileSync`, its `fsync`, the handle puts every dirty chunk, every truncated chunk and the new metadata record into one SlateDB write batch, then flushes it to object storage before returning.

That gives us two guarantees:

- When `FileSync` returns, the data is durable in the bucket. DuckDB's commit and checkpoint durability rules carry over unchanged.
- No reader can ever see a file size that the stored chunks don't back.

Metadata changes are SlateDB transactions. During a checkpoint, DuckDB moves `db.wal.checkpoint` onto `db.wal`. For us, that rename is a single transaction: it re-points the `db.wal` path key to the other file ID and deletes the old file's chunks. No data is copied, and no reader can observe the rename half done.

### One writer, many readers

![An ingest job writes to the bucket while dashboards, notebooks and API services attach read-only. A second writer would take over the manifest and fence the first.](/assets/images/duckdb-object-storage/readers-writer.svg)

Each database has at most one writer and any number of readers:

- **The writer** opens a SlateDB database when a file is first opened for writing, which in practice means a read-write `ATTACH`. Its durable batches are the only way the database changes.
- **Readers** use SlateDB's non-fencing reader, so attaching `READ_ONLY` from another process never disturbs the writer. In the writer's own process, read-only attachments share the writer's instance and always see the latest commit.
- **A second writer** that shouldn't be there is caught by SlateDB's manifest fencing. The newer writer takes over, and the older one gets an I/O error on its next flush instead of quietly diverging.

The principle behind all of this: **a reader only ever sees a state the writer committed.** A reader works from a SlateDB checkpoint of the writer's manifest, and it moves to a newer checkpoint as a whole. It never sees half of a write batch, and it never sees a block from one commit next to a block from another. Freshness is bounded rather than immediate: a reader picks up new commits about every ten seconds, when it polls the manifest.

We checked this with two processes. A writer rewrote a 40-million-row table and checkpointed six times in a row, while a long-lived read-only attachment, with a 64 MB memory limit so it kept rereading from storage, summed the table every few seconds. Every result it returned was exactly the sum of one committed version: the original, then the fourth rewrite, then the sixth and final one. It never returned anything in between.

> Readers can be a little behind. They are never inconsistent.

That maps onto a common deployment: one job writes, and a fleet of stateless processes reads the same database straight from the bucket.

### Fewer, larger, concurrent requests

![The chunks a read needs are grouped into contiguous runs. Each run becomes one range scan or point lookup, issued in parallel and served from cache before the bucket.](/assets/images/duckdb-object-storage/read-path.svg)

Object storage charges for every request in latency, so the read path tries to make fewer and bigger ones. When DuckDB asks for a byte range, we work out which chunks it touches, group them into contiguous runs, and fetch each run with one SlateDB range scan, or one point lookup for a single chunk. All runs are fetched in parallel. Read-ahead is set to the bytes actually requested, capped at 4 MiB, so SlateDB can merge neighboring SST blocks without fetching data nobody asked for. Positional reads from DuckDB's worker threads run concurrently all the way through the FFI.

In front of the bucket sits an in-memory cache, 512 MiB for data blocks and 128 MiB for SST metadata by default. An optional local disk cache keeps SST parts across restarts, which helps most with S3.

### Fitting into DuckDB

A few smaller choices make the extension behave like part of DuckDB rather than something attached to it:

- **Credentials come from DuckDB's Secret Manager.** The extension reads the standard `TYPE S3` secret, with scopes, `credential_chain`, custom endpoints for MinIO, and anonymous access for public buckets. Credentials never go into extension settings.
- **Only durable files go to the bucket.** The database file and its WAL files go to object storage. Spill files don't: if `temp_directory` points at a `duckdb_objfs://` path, the extension moves it to a local temporary directory. Extensions, secrets, logs and `COPY` output follow their own paths.
- **Settings fail loudly.** Backend, bucket and cache settings are read once, at the first `duckdb_objfs://` access. Changing them afterwards is an error rather than a silent no-op.
- **You can see what it's doing.** `duckdb_objfs_cache_stats()` reports cache hits, misses and evictions. `duckdb_objfs_io_stats()` reports request counts, latency and payload bytes for each kind of object-storage operation.

## The storage layer for distributed DuckDB

![A client sends a query to a driver that is the only writer. The driver fans partitioned tasks out to read-only workers, which all read the same database from one bucket.](/assets/images/duckdb-object-storage/distributed.svg)

This extension isn't only for single-node DuckDB. It's also the prerequisite for [Duckherder](https://github.com/dentiny/duckdb-distributed-execution), our distributed execution extension for DuckDB. In Duckherder, a driver plans a query, splits it into partitioned tasks, sends them to workers over Arrow Flight, and merges the partial results.

Distributed execution needs every worker to read the same data without copying it first, and that is exactly the model described above:

- **The driver owns the only writer.** DDL, DML and checkpoints go through it, and it publishes the results to the bucket.
- **Every worker is a read-only attachment** of the same `duckdb_objfs://` database. Adding a worker means starting another reader: no data to copy, no shards to rebalance, no local disks to fill.
- **Snapshot-consistent reads** mean a worker never computes over a half-written state.

Duckherder already distributes partial aggregation over a database stored this way. The next step is to pin each distributed query to one committed snapshot, so every worker reads exactly the same version. That work is in progress on both sides.

## The test suite is the spec

It's easy to say "everything works the same." We wanted DuckDB to check that for us.

So we run DuckDB's own SQLLogicTest suite (thousands of test files) with every test redirected into an attached `duckdb_objfs://` database, on both the local-disk and memory backends. Tests that don't pass have to go into one of three skip lists, each with a written reason:

- **By design (about 45 DuckDB tests).** These assume the default catalog is called `memory`, or count the attached databases. They fail the same way with any attached database, including a plain local file.
- **Unsupported (10 tests).** These open a `.duckdb` fixture file that was never written through `duckdb_objfs://`.
- **Extension bugs: none.** The file exists and is empty. Any new entry must link to a tracking issue.

A `make` target also fails if a skipped test starts passing, so the skip lists can't quietly go stale. Below that, Rust unit tests cover chunking, truncation, fault injection and a fake S3 server, and an end-to-end suite runs against a local S3 service.

## The numbers

![Bar chart of ObjFS time relative to native DuckDB: 0.92× for memory-backend reads, 1.08× for local-disk reads, 1.20× for cold S3 reads, and 1.05× for writing and delivering to S3.](/assets/images/duckdb-object-storage/benchmark-summary.svg)

All measurements were taken on an Apple M4 with DuckDB v1.5.5. Read numbers are TPC-H Q1–Q22.

- **Memory backend, SF10.** The geometric mean is 0.92× native in-memory DuckDB, and 20 of 22 queries were faster. The layer adds little overhead when storage itself is fast.
- **Local disk, SF10.** The geometric mean is 1.08× a native `.duckdb` file, and most queries are within 10%. The worst query, Q5, is 1.40×.
- **S3, SF1, cold process, memory cache only.** The geometric mean is 1.20× native DuckDB reading a `.duckdb` file through HTTPFS, ranging from 0.92× to 1.58×. Every query started a fresh process with empty caches.
- **Writing to S3, SF1 `lineitem`.** Writing directly through the extension reached 0.95× the throughput of writing natively to local disk and uploading the file with the AWS CLI. You get the same result, without the local copy or the upload step.

Per-query tables and the full method are in [`benchmark.md`](https://github.com/dentiny/duckdb-object-storage/blob/main/benchmark.md).

## What it costs

This isn't free, and we'd rather tell you here than have you find out in production.

- **One writer per database.** Exclusivity isn't enforced by a lock. A second read-write attach doesn't fail immediately; fencing stops the first writer later. Run a single writer, or coordinate writers outside the extension.
- **Every commit is a round trip to the bucket.** `fsync` returns only once the batch is durable in object storage, so on S3 each commit pays a PUT's latency, typically tens of milliseconds rather than the sub-millisecond fsync of a local SSD. Batch small writes into fewer transactions.
- **Readers can lag by about ten seconds.** They're always consistent, but not always current. That includes a `DETACH`/`ATTACH` cycle in the same process, which reuses the cached reader.
- **The writer does background work.** SlateDB's compaction and garbage collection run inside the writer process and use its CPU, memory and request budget.
- **Local-disk writes are slow.** Building SF10 `lineitem` and checkpointing ran at 0.19× native throughput. On S3 the gap disappears, because native DuckDB has to upload afterwards anyway, but on local disk it's the biggest cost we have. Logging every commit twice is a likely part of it; see "What comes next."
- **Cold S3 reads are about 20% slower** than native DuckDB reading a `.duckdb` file over HTTPFS, with the worst query at 1.58×.
- **The bucket doesn't hold a `.duckdb` file.** It holds SlateDB SSTs, WAL objects and manifests. You can't `aws s3 cp` the database somewhere and open it with stock DuckDB. To get data out, attach both databases and use `COPY FROM DATABASE` or `EXPORT DATABASE`. To bring an existing database in, copy it the same way.
- **It's DuckDB-only.** If Spark and Trino need to read the same tables, that's what Iceberg and DuckLake are for.
- **Directory listing isn't implemented yet.** `Glob`, `ListFiles` and `DirectoryExists` are missing, so features that list `duckdb_objfs://` directories don't work yet.
- **Settings are global per process** and fixed at the first `duckdb_objfs://` access.

## What comes next

Three of the next steps are design changes rather than tuning, so they get more than a bullet.

### A strong consistency mode

Readers are snapshot-consistent today, but their freshness is bounded, not immediate. That's the right default for dashboards and fleets of readers, but some workloads need read-after-write across processes: a job that commits and then hands off to a reader, or a distributed query that has to see the commit that came just before it.

We plan to add an opt-in strong consistency mode. In that mode, a read-only transaction first refreshes to the writer's latest manifest, so it sees every commit made before it started. Each refresh costs a round trip to the bucket, which is why it will stay opt-in. Together with snapshot pinning, this lets Duckherder ask for "the latest commit, and the same one on every worker."

### One write-ahead log, not two

Today a commit passes through two write-ahead logs. DuckDB writes its own WAL (`analytics.db.wal`) and calls `fsync` on commit. To us, that WAL is just another file, so its bytes become chunks in a SlateDB write batch, and SlateDB appends that batch to *its* WAL before it reaches the memtable and, eventually, an SST. At checkpoint, DuckDB writes the changed blocks into the database file, which goes through SlateDB's WAL again, and then retires its own WAL.

Each log is doing its job correctly. Together, they log the same commit twice and pay for two durability points, and that is a likely contributor to the local write gap. We're going to redesign this so a single log provides durability. Either SlateDB's WAL becomes the commit log and DuckDB's WAL no longer needs to be persisted separately, or DuckDB's WAL stays authoritative and block writes skip SlateDB's WAL. Most of the design work is deciding which side owns durability and how recovery replays it.

### From the filesystem to the block manager

We plug in at DuckDB's `FileSystem` layer because it was the most convenient place to start. DuckDB already routes every database file through it, so a virtual filesystem could store real databases from day one without changing DuckDB at all. The cost is that we see bytes and offsets, not blocks. That's why we had to work backwards from DuckDB's file layout, using the 12 KiB header offset, to get one key per block.

DuckDB has a better seam one level up. Its storage goes through an abstract [`BlockManager`](https://github.com/duckdb/duckdb/blob/069cc9f9b5be802405797faecc284961b07c70ef/src/include/duckdb/storage/block_manager.hpp#L29-L101), which reads and writes whole blocks by block ID, allocates and frees blocks, writes the database header, and syncs. The on-disk implementation, [`SingleFileBlockManager`](https://github.com/duckdb/duckdb/blob/069cc9f9b5be802405797faecc284961b07c70ef/src/include/duckdb/storage/single_file_block_manager.hpp#L58-L61), lays those blocks out at byte offsets in a single file, and our filesystem layer then maps the offsets back into blocks.

A block manager backed directly by SlateDB would remove that round trip and give us much more flexibility:

- **Blocks keyed by block ID**, with no offset arithmetic and no file layout to work backwards from.
- **Block size and layout chosen for object storage**, rather than inherited from a single-file format.
- **Checkpoints as one transaction** over the blocks they write and free. Freed blocks become deletes instead of a file truncation.
- **A natural place to merge the two WALs**, because commit and checkpoint semantics are visible at that layer.
- **Block-level versioning**, which would make point-in-time reads and cheap database branching, built on SlateDB checkpoints and clones, much easier.

There's a catch. DuckDB constructs `SingleFileBlockManager` directly inside its storage manager ([here](https://github.com/duckdb/duckdb/blob/069cc9f9b5be802405797faecc284961b07c70ef/src/storage/storage_manager.cpp#L439) and [here](https://github.com/duckdb/duckdb/blob/069cc9f9b5be802405797faecc284961b07c70ef/src/storage/storage_manager.cpp#L467)), so an extension can't supply its own block manager today. Getting there means either a storage extension that brings its own storage manager, or a small extension point upstream. The filesystem layer got us a working system first. The block manager is where we'd like to end up.

### Also on the list

- **Faster local writes.** Beyond the single WAL: fewer copies of staged chunks, batching across file handles, and SlateDB flush and compaction settings tuned for DuckDB's write pattern.
- **Faster remote reads.** Prefetching that follows DuckDB's scan pattern, better persistent-cache defaults, and warming the cache on attach.
- **Fail-fast writer exclusivity.** A second read-write attach should be rejected right away, not fenced later.
- **Snapshot pinning.** Let a query, or every worker in a distributed query, read one named committed version.
- **A configurable manifest polling interval** for readers that don't need the strong mode but want fresher data than the default.
- **Directly importing existing `.duckdb` files**, which would also empty the "unsupported" skip list.
- **Directory listing**, so globbing over `duckdb_objfs://` paths works.
- **More backends through OpenDAL.** GCS, Azure Blob Storage and HDFS are mostly feature flags and configuration, with credentials coming from DuckDB secrets.
- **Per-database configuration**, so different `ATTACH` statements can use different buckets or backends.

## Takeaways

- **DuckDB can separate compute from storage without giving up being a database.** Indexes, constraints, the WAL, the catalog and the extension ecosystem all come along, because the storage engine never knows anything changed.
- **The deployment is a bucket.** There's no catalog service, no metadata database and no compactor. Everything, including writer fencing, lives in object storage.
- **One block is one key, one `fsync` is one durable batch, and readers only see committed states.** Those three rules carry most of the design.
- **It's the foundation for distributed DuckDB.** One writer and many snapshot-consistent readers is exactly what Duckherder's workers need.
- **It isn't trying to replace table formats.** Use it for databases DuckDB owns, use Iceberg or DuckLake for tables many engines share, and attach all of them in one session when you need both.

Here's what it comes down to: start DuckDB anywhere, attach a database from a bucket, and get the same database you would have on a laptop, with nothing else to run.

```sql
INSTALL duckdb_object_storage FROM community;
LOAD duckdb_object_storage;
ATTACH 'duckdb_objfs://hello.db' AS db;   -- local disk by default; set the backend to 's3' for a bucket
```

Source, issues and benchmarks are on [GitHub](https://github.com/dentiny/duckdb-object-storage).

## Appendix: reproducing the benchmarks

The read benchmark compares native DuckDB with the extension on TPC-H. It builds a release binary, generates data with `dbgen`, and records DuckDB's JSON profiles per query.

```sh
# Local: memory and local-disk backends, SF10, Q1–Q22
python3 scripts/run_read_benchmarks.py --scale-factor 10 --runs 5

# S3: SF1, a fresh process per query, three runs, no warm-up
python3 scripts/run_read_benchmarks.py --remote --scale-factor 1 \
  --s3-bucket <bucket> --s3-region <region> --aws-profile <profile>

# Writes: CREATE TABLE AS SELECT of lineitem plus CHECKPOINT
python3 scripts/run_write_benchmarks.py --local --scale-factor 10
python3 scripts/run_write_benchmarks.py --remote \
  --s3-bucket <bucket> --s3-region <region> --aws-profile <profile>
```

Setup for the published numbers:

- **Client:** Apple M4 (10 cores, 16 GB), DuckDB v1.5.5, default thread count and memory limit.
- **Local reads:** SF10, one warm-up and five measured runs per query.
- **S3 reads:** SF1, with the client in Taipei reading over the internet from a bucket in `ap-east-2`, three cold-process runs per query. The extension used its default in-memory caches with the persistent cache disabled. Native DuckDB used HTTPFS with its external file cache enabled, starting empty in each process.
- **Writes:** three fresh databases per backend. Source data is generated in memory before timing. Native S3 delivery includes the upload with the AWS CLI.

Local SF10 and remote SF1 are separate runs at different scale factors and shouldn't be compared directly. This is TPC-H, not a promise about your workload.
