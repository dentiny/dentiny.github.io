"""Reproduce the post's charts from its frozen official download snapshot.

Run with Python 3 and matplotlib installed:
    python scripts/plot-extension-downloads.py
"""
import datetime as dt
import json
import textwrap
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.patches import Patch, FancyBboxPatch
from matplotlib.ticker import FuncFormatter

ROOT = Path(__file__).resolve().parents[1]
DATA = json.loads((ROOT / 'scripts/duckdb-extensions-downloads-2026-10-12.json').read_text())
OUTPUT = ROOT / 'assets/images/duckdb-extensions'
COLORS = {'Remote reads': '#ea580c', 'Storage': '#1971c2', 'Other systems': '#2b9e43',
          'Flaky IO': '#7048e8', 'Observe': '#0b8799'}
LABELS = {'Remote reads': 'Make remote reads fast', 'Storage': 'Reach more storage',
          'Other systems': 'Connect other systems', 'Flaky IO': 'Tame slow, flaky IO',
          'Observe': 'Observe and govern'}
BG = '#fffdf8'
plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 12,
                     'text.color': '#25292b', 'axes.labelcolor': '#25292b',
                     'axes.edgecolor': '#8a949d', 'xtick.color': '#25292b',
                     'ytick.color': '#25292b', 'figure.facecolor': BG, 'axes.facecolor': BG})

def style(ax):
    ax.spines[['top', 'right']].set_visible(False)
    ax.set_axisbelow(True)

extensions = DATA['extensions']
archives = DATA['archives']
totals = {e['name']: sum(s['downloads'].get(e['name'], 0) for s in archives) for e in extensions}
total = sum(totals.values())
latest_total = sum(DATA['latest']['downloads'].values())
assert len(extensions) == 18
assert len({s['last_update'] for s in archives}) == len(archives)

fig, ax = plt.subplots(figsize=(15, 10.5))
fig.subplots_adjust(left=.23, right=.89, top=.84, bottom=.10)
fig.text(.025, .955, f'{total:,} downloads in the available weekly archives', fontsize=23, weight='bold')
fig.text(.23, .89, 'Archived snapshots through Oct 11, 2026. Bold = highlighted. Missing weeks are not estimated.',
         fontsize=11, color='#7d8993')
ordered = sorted((e for e in extensions if totals[e['name']] > 0), key=lambda e: totals[e['name']], reverse=True)
for i, e in enumerate(ordered):
    count = totals[e['name']]
    ax.barh(i, count, color=COLORS[e['group']], height=.72)
    ax.text(count * 1.10, i, f'{count:,}', va='center', fontsize=11,
            weight='bold' if e['highlight'] else 'normal')
ax.set_yticks(range(len(ordered)), [e['name'] for e in ordered], fontfamily='DejaVu Sans Mono')
for label, e in zip(ax.get_yticklabels(), ordered):
    label.set_fontweight('bold' if e['highlight'] else 'normal')
ax.invert_yaxis()
ax.set_xscale('log')
ax.set_xlim(200, 4_000_000)
ax.set_xlabel('Downloads summed over archived weekly snapshots (log scale)', labelpad=12)
ax.grid(axis='x', color='#e4e9ed')
style(ax)
ax.legend(handles=[Patch(color=c, label=LABELS[g]) for g, c in COLORS.items()], loc='lower right', frameon=False, fontsize=10)
fig.text(.23, .025, 'compression_fs: no published count. Source: community-extensions.duckdb.org', fontsize=10, color='#7d8993')
fig.savefig(OUTPUT / 'downloads-by-extension.png', dpi=160)
plt.close(fig)

fig, ax = plt.subplots(figsize=(15, 7.5))
fig.subplots_adjust(left=.09, right=.98, top=.80, bottom=.13)
fig.text(.025, .95, 'Weekly downloads across all 18 extensions', fontsize=24, weight='bold')
fig.text(.09, .88, f'Latest rolling week: {latest_total:,} downloads (updated Oct 12, 2026, 04:00 UTC).', fontsize=12, color='#7d8993')
fig.text(.09, .84, 'Bars: weekly archives through Oct 11. Grey bands: unavailable snapshots. Rolling week is not added to the bars.',
         fontsize=10, color='#7d8993')
dates = [dt.datetime.fromisoformat(s['last_update'].replace('Z', '+00:00')) for s in archives]
bottom = [0] * len(dates)
for group, color in COLORS.items():
    names = [e['name'] for e in extensions if e['group'] == group]
    values = [sum(s['downloads'].get(n, 0) for n in names) for s in archives]
    ax.bar(dates, values, bottom=bottom, width=5.8, color=color, label=LABELS[group])
    bottom = [a + b for a, b in zip(bottom, values)]
for missing in DATA['missing_archives']:
    sunday = dt.datetime.combine(dt.date.fromisocalendar(missing['year'], missing['week'], 7), dt.time(), tzinfo=dt.timezone.utc)
    ax.axvspan(sunday - dt.timedelta(days=3.5), sunday + dt.timedelta(days=3.5), color='#f0f3f5', zorder=0)
ax.set_xlim(dates[0] - dt.timedelta(days=7), dates[-1] + dt.timedelta(days=14))
ax.set_ylabel('Downloads per weekly snapshot')
ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: '0' if v == 0 else f'{v / 1000:.0f}k'))
ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
ax.grid(axis='y', color='#e4e9ed')
ax.legend(loc='upper left', frameon=False, fontsize=10)
style(ax)
fig.text(.09, .03, 'Source: community-extensions.duckdb.org. Snapshot fetched Oct 12, 2026 (PDT).', fontsize=10, color='#7d8993')
fig.savefig(OUTPUT / 'weekly-downloads.png', dpi=160)
plt.close(fig)
print(f'Archive total: {total:,}; latest rolling week: {latest_total:,}')

# The overview map also includes per-extension and per-group download counts.
DESCRIPTIONS = {
    'cache_httpfs': 'Block/metadata/glob cache for remote files',
    'cache_prewarm': 'pg_prewarm for DuckDB, local and remote',
    'query_condition_cache': 'Predicate cache, up to 29x on repeated filters',
    'duckdb_opendalfs': 'S3 / GCS / Azure / HF / SFTP via Apache OpenDAL',
    'duckdb_object_storage': 'Writable .duckdb on object storage via SlateDB',
    'curl_httpfs': 'httpfs on libcurl: HTTP/2, pooling, async IO',
    'compression_fs': 'LZ4, Snappy, Brotli, Bzip2, XZ files',
    'duckherder': 'Remote & distributed execution over Arrow Flight',
    'lance_conversion': 'COPY query to Lance, with indexes',
    'huggingface': 'Discover, scan, profile HF datasets',
    'httpfs_timeout_retry': 'Per-operation timeout and retry',
    'rate_limit_fs': 'GCRA rate and burst limits',
    'hedged_request_fs': 'Hedged requests to cut tail latency',
    'latency_injection_fs': 'Log-normal latency injection',
    'observefs': 'IO latency histograms per bucket',
    'system_stats': 'CPU / memory / disk as tables',
    'table_inspector': 'Storage internals per table/column',
    'query_limiter': 'Reject queries over a row budget',
}
MAP_ORDER = {
    'Remote reads': ['cache_httpfs', 'cache_prewarm', 'query_condition_cache'],
    'Storage': ['duckdb_opendalfs', 'duckdb_object_storage', 'curl_httpfs', 'compression_fs'],
    'Other systems': ['duckherder', 'lance_conversion', 'huggingface'],
    'Flaky IO': ['httpfs_timeout_retry', 'rate_limit_fs', 'hedged_request_fs', 'latency_injection_fs'],
    'Observe': ['observefs', 'system_stats', 'table_inspector', 'query_limiter'],
}

def compact(value):
    if value >= 1_000_000:
        return f'{value / 1_000_000:.1f}M'
    if value >= 1_000:
        return f'{value / 1_000:.1f}k'
    return str(value)

fig, ax = plt.subplots(figsize=(20, 11.5))
fig.subplots_adjust(left=.02, right=.99, top=.98, bottom=.02)
ax.set_xlim(-.5, 100)
ax.set_ylim(0, 100)
ax.axis('off')
ax.text(0, 96, '18 DuckDB community extensions, by what you need', fontsize=26, weight='bold')
ax.text(0, 91, 'Built and maintained by Hao Jiang (@dentiny) and collaborators', fontsize=14, color='#7d8993')
ax.text(0, 87, f'{compact(total)} archive downloads · Thick borders = highlights · NEW = released in the last six weeks',
        fontsize=13, color='#7d8993')
for column, (group, names) in enumerate(MAP_ORDER.items()):
    x = column * 20
    color = COLORS[group]
    ax.add_patch(FancyBboxPatch((x, 72), 19, 10, boxstyle='round,pad=0.15,rounding_size=0.7', color=color))
    ax.text(x + .7, 78.5, LABELS[group], color='white', fontsize=15, weight='bold')
    ax.text(x + .7, 74.2, f'{len(names)} extensions · {compact(sum(totals[n] for n in names))} downloads', color='white', fontsize=12)
    for row, name in enumerate(names):
        e = next(e for e in extensions if e['name'] == name)
        y = 53 - row * 16
        ax.add_patch(FancyBboxPatch((x, y), 19, 14.5, boxstyle='round,pad=0.15,rounding_size=0.7',
                                  facecolor='white', edgecolor=color, linewidth=3 if e['highlight'] else 1.3))
        ax.text(x + .7, y + 11.8, name, fontfamily='DejaVu Sans Mono', fontsize=12.5, weight='bold')
        description = '\n'.join(textwrap.wrap(DESCRIPTIONS[name], width=34))
        ax.text(x + .7, y + 8.2, description, va='top', fontsize=11, linespacing=1.5, color='#56616a')
        count = compact(totals[name]) if totals[name] else 'no published count'
        ax.text(x + .7, y + 1.5, count, fontsize=12, weight='bold', color=color)
        badge = 'EXPERIMENTAL' if name == 'duckherder' else ('NEW' if e['first_release'] == '2026-09' else '')
        if badge:
            ax.text(x + 18.4, y + 1.5, badge, ha='right', fontsize=9, weight='bold', color='white',
                    bbox=dict(boxstyle='round,pad=.2', facecolor='#dc3030' if badge == 'EXPERIMENTAL' else '#2b9140', edgecolor='none'))
ax.text(0, 1, 'Downloads: available weekly archives through Oct 11, 2026. Missing weeks are not estimated; not exact lifetime totals.',
        fontsize=11, color='#7d8993')
fig.savefig(OUTPUT / 'extension-map.png', dpi=160)
plt.close(fig)
