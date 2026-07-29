# Apple Music Dashboard

A running, self-updating "Spotify Wrapped" for my own Apple Music library —
daily snapshots of every track's play/skip counts, warehoused in SQLite, and
visualized in Tableau.

## Architecture

```
launchd (daily @ 09:00)
   -> ingest_snapshot.py
        -> export_music_library.js   (JXA: reads Music.app's track library)
        -> am_dashboard.sqlite       (star schema: dim_track + fact_usage)
        -> daily_usage.csv           (flat export, rebuilt each run)
                -> Apple Music Dashboard.twbx   (Tableau Public)
```

- **`export_music_library.js`** — a JXA (JavaScript for Automation) script.
  Enumerates every track in Music.app and prints it as JSON. Run standalone
  with `osascript -l JavaScript export_music_library.js`.
- **`ingest_snapshot.py`** — the daily ETL. Pulls the JXA export, upserts
  track metadata into `dim_track`, and appends one `fact_usage` row per
  track per day with `play_count_delta` / `skip_count_delta` computed
  against the previous snapshot. Rebuilds `daily_usage.csv` from the full
  join each run.
- **`am_dashboard.sqlite`** — two-table star schema:
  - `dim_track` — current metadata per track, keyed on Apple's persistent ID.
  - `fact_usage` — one row per `(persistent_id, snapshot_date)`, with
    running totals and day-over-day deltas.
- **`launchd/`** — a template for the LaunchAgent that triggers the daily
  run (the real, machine-specific plist lives in `~/Library/LaunchAgents`,
  not in this repo — see Setup below).

## Robustness notes

- **Missed runs**: `launchd`'s `StartCalendarInterval` fires once at a fixed
  time; if the Mac is asleep, off, or (as happened once) in for repair, that
  day is skipped and can't be caught up automatically — Apple Music only
  exposes *current* totals, not history. `ingest_snapshot.py` detects any
  gap since the last successful snapshot and logs it explicitly on the next
  run, instead of failing silently.
- **Partial reads**: occasionally the JXA export returns a smaller-than-
  expected track count (Music.app/iCloud not fully warmed up at 09:00). A
  hard floor (`MIN_TRACKS`) aborts the snapshot if the count is absurdly
  low; a softer threshold (`PARTIAL_READ_DROP_THRESHOLD`) just logs a
  warning for smaller dips, since these have consistently self-corrected
  the next day without manual intervention.
- **Negative deltas**: if a track's play/skip counter ever decreases
  (counter resets, ID reuse), the delta is clamped to 0 and logged rather
  than written as a negative number.
- **Atomicity**: all `dim_track`/`fact_usage` writes for a run happen in one
  transaction — a failure partway through rolls back the whole snapshot
  rather than leaving a half-written day in the database.
- **Failure visibility**: `main()` catches any exception, logs it (with
  traceback) to `logs/stderr.log`, and writes `.last_run_status.json` with
  the outcome of the most recent run — a cheap freshness/health check
  without needing to grep logs.

## Setup

1. Requires macOS with Music.app and Python 3 (stdlib only, no dependencies).
2. Clone this repo.
3. Grant the Terminal/`osascript` automation permission to control
   Music.app when first prompted (System Settings → Privacy & Security →
   Automation).
4. Copy `launchd/com.example.applemusic.snapshot.plist.template` to
   `~/Library/LaunchAgents/com.<you>.applemusic.snapshot.plist`, fill in the
   `__PLACEHOLDER__` paths, then:
   ```
   launchctl load ~/Library/LaunchAgents/com.<you>.applemusic.snapshot.plist
   ```
5. `am_dashboard.sqlite`, `daily_usage.csv`, and `logs/` are gitignored —
   they're generated locally on first run and contain personal listening
   data.

## Dashboard

Built in Tableau Public against `daily_usage.csv` / `am_dashboard.sqlite`:
top tracks, cumulative play history, genre-by-day-of-week, and plays by
release decade. Published version: _link TBD_.
