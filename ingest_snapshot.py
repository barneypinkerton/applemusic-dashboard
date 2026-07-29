# ingest_snapshot.py
import subprocess, json, sqlite3, pathlib, datetime, csv, os, sys, traceback

BASE = pathlib.Path(__file__).resolve().parent
DB_PATH = BASE / 'am_dashboard.sqlite'
JS_PATH = BASE / 'export_music_library.js'
LOG_DIR = BASE / 'logs'
LOG_DIR.mkdir(parents=True, exist_ok=True)
STATUS_PATH = BASE / '.last_run_status.json'

SNAPSHOT_DATE = datetime.date.today().isoformat()
MIN_TRACKS = 500
# A single day's export coming back >5% short of the last known snapshot is
# treated as a partial read (Music.app/iCloud not fully warm at launchd time)
# rather than a real library shrink.
PARTIAL_READ_DROP_THRESHOLD = 0.05

DDL_DIM_TRACK = """
CREATE TABLE IF NOT EXISTS dim_track (
  persistent_id TEXT PRIMARY KEY,
  name TEXT,
  artist TEXT,
  album TEXT,
  album_artist TEXT,
  composer TEXT,
  genre TEXT,
  grouping TEXT,
  comments TEXT,
  year INTEGER,
  bpm INTEGER,
  rating INTEGER,
  loved INTEGER,
  disliked INTEGER,
  duration_sec REAL,
  track_number INTEGER,
  track_count INTEGER,
  disc_number INTEGER,
  disc_count INTEGER,
  kind TEXT,
  bit_rate INTEGER,
  sample_rate INTEGER,
  size_bytes INTEGER,
  date_added TEXT,
  modification_date TEXT,
  compilation INTEGER,
  explicit INTEGER,
  cloud_status TEXT,
  location TEXT,
  first_seen_snapshot TEXT,
  last_seen_snapshot TEXT
);
"""

DDL_FACT_USAGE = """
CREATE TABLE IF NOT EXISTS fact_usage (
  persistent_id TEXT NOT NULL,
  snapshot_date TEXT NOT NULL,
  play_count_total INTEGER NOT NULL,
  skip_count_total INTEGER NOT NULL,
  last_played TEXT,
  last_skipped TEXT,
  play_count_delta INTEGER NOT NULL,
  skip_count_delta INTEGER NOT NULL,
  PRIMARY KEY (persistent_id, snapshot_date),
  FOREIGN KEY (persistent_id) REFERENCES dim_track(persistent_id)
);
"""

DDL_IDX = [
  "CREATE INDEX IF NOT EXISTS idx_fact_date ON fact_usage(snapshot_date)",
  "CREATE INDEX IF NOT EXISTS idx_fact_pid ON fact_usage(persistent_id)"
]

def run_jxa():
  with open(LOG_DIR / 'stderr.log', 'a') as err_fh:
    out = subprocess.check_output(
        ['/usr/bin/osascript', '-l', 'JavaScript', str(JS_PATH)],
        stderr=err_fh
    )
  data = json.loads(out.decode('utf-8'))
  if len(data) < MIN_TRACKS:
    raise RuntimeError(f"Suspiciously few tracks returned: {len(data)} — aborting snapshot")
  return data


def check_for_gap(conn):
  """Log (but don't fail on) any missed days since the last successful snapshot."""
  row = conn.execute("SELECT MAX(snapshot_date) FROM fact_usage").fetchone()
  last_date = row[0] if row and row[0] else None
  if not last_date:
    return
  last = datetime.date.fromisoformat(last_date)
  today = datetime.date.fromisoformat(SNAPSHOT_DATE)
  missed = (today - last).days - 1
  if missed > 0:
    print(f"[WARN] {missed} day(s) missed between {last_date} and {SNAPSHOT_DATE} "
          f"(no snapshot taken — Mac likely off/asleep/unreachable). Deltas for that "
          f"span were not captured and cannot be backfilled.")


def check_for_partial_read(conn, track_count):
  row = conn.execute("SELECT COUNT(*) FROM fact_usage WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM fact_usage)").fetchone()
  prev_count = row[0] if row else 0
  if prev_count and track_count < prev_count * (1 - PARTIAL_READ_DROP_THRESHOLD):
    print(f"[WARN] track count dropped {prev_count} -> {track_count} "
          f"({(1 - track_count / prev_count):.1%}) — likely a partial library read, "
          f"not a real library shrink. Proceeding; expect this to self-correct next run.")

def ensure_schema(conn):
  c = conn.cursor()
  c.execute(DDL_DIM_TRACK)
  c.execute(DDL_FACT_USAGE)
  for stmt in DDL_IDX:
    c.execute(stmt)
  conn.commit()

def upsert_dim_track(c, r):
  c.execute("""
    INSERT INTO dim_track (
      persistent_id, name, artist, album, album_artist, composer, genre, grouping, comments,
      year, bpm, rating, loved, disliked, duration_sec, track_number, track_count, disc_number, disc_count,
      kind, bit_rate, sample_rate, size_bytes, date_added, modification_date, compilation, explicit,
      cloud_status, location, first_seen_snapshot, last_seen_snapshot
    ) VALUES (
      :persistent_id, :name, :artist, :album, :album_artist, :composer, :genre, :grouping, :comments,
      :year, :bpm, :rating, :loved, :disliked, :duration_sec, :track_number, :track_count, :disc_number, :disc_count,
      :kind, :bit_rate, :sample_rate, :size_bytes, :date_added, :modification_date, :compilation, :explicit,
      :cloud_status, :location, :first_seen_snapshot, :last_seen_snapshot
    )
    ON CONFLICT(persistent_id) DO UPDATE SET
      name=excluded.name,
      artist=excluded.artist,
      album=excluded.album,
      album_artist=excluded.album_artist,
      composer=excluded.composer,
      genre=excluded.genre,
      grouping=excluded.grouping,
      comments=excluded.comments,
      year=excluded.year,
      bpm=excluded.bpm,
      rating=excluded.rating,
      loved=excluded.loved,
      disliked=excluded.disliked,
      duration_sec=excluded.duration_sec,
      track_number=excluded.track_number,
      track_count=excluded.track_count,
      disc_number=excluded.disc_number,
      disc_count=excluded.disc_count,
      kind=excluded.kind,
      bit_rate=excluded.bit_rate,
      sample_rate=excluded.sample_rate,
      size_bytes=excluded.size_bytes,
      date_added=excluded.date_added,
      modification_date=excluded.modification_date,
      compilation=excluded.compilation,
      explicit=excluded.explicit,
      cloud_status=excluded.cloud_status,
      location=excluded.location,
      last_seen_snapshot=excluded.last_seen_snapshot
  """, {**r, 'first_seen_snapshot': SNAPSHOT_DATE, 'last_seen_snapshot': SNAPSHOT_DATE})

def get_prev_totals(c, pid):
  row = c.execute(
    "SELECT play_count_total, skip_count_total FROM fact_usage WHERE persistent_id=? ORDER BY snapshot_date DESC LIMIT 1",
    (pid,)
  ).fetchone()
  return row if row else (None, None)

def insert_fact(c, r):
  pid = r['persistent_id']
  prev_play, prev_skip = get_prev_totals(c, pid)
  play_total = int(r.get('play_count_total') or 0)
  skip_total = int(r.get('skip_count_total') or 0)
  play_delta = play_total - (prev_play if prev_play is not None else play_total)
  skip_delta = skip_total - (prev_skip if prev_skip is not None else skip_total)
  if prev_play is not None and play_delta < 0:
    print(f"[WARN] negative play delta for {pid}: {prev_play} → {play_total}, clamped to 0")
    play_delta = 0
  if prev_skip is not None and skip_delta < 0:
    print(f"[WARN] negative skip delta for {pid}: {prev_skip} → {skip_total}, clamped to 0")
    skip_delta = 0

  c.execute("""
    INSERT OR REPLACE INTO fact_usage (
      persistent_id, snapshot_date, play_count_total, skip_count_total,
      last_played, last_skipped, play_count_delta, skip_count_delta
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
  """, (pid, SNAPSHOT_DATE, play_total, skip_total, r.get('last_played'),
        r.get('last_skipped'), play_delta, skip_delta))

def write_status(status, detail=""):
  STATUS_PATH.write_text(json.dumps({
    "status": status,
    "snapshot_date": SNAPSHOT_DATE,
    "ran_at": datetime.datetime.now().isoformat(timespec='seconds'),
    "detail": detail,
  }, indent=2))

def run_snapshot():
  data = run_jxa()
  conn = sqlite3.connect(DB_PATH)
  ensure_schema(conn)
  check_for_gap(conn)
  check_for_partial_read(conn, len(data))
  try:
    c = conn.cursor()
    for r in data:
      r['loved'] = 1 if r.get('loved') else 0
      r['disliked'] = 1 if r.get('disliked') else 0
      r['compilation'] = 1 if r.get('compilation') else 0
      r['explicit'] = 1 if r.get('explicit') else 0
      upsert_dim_track(c, r)
      insert_fact(c, r)
    conn.commit()
  except Exception:
    conn.rollback()
    raise
  finally:
    conn.close()

  # --- CSV export: rebuild from whole table each run (idempotent) ---
  csv_path = BASE / 'daily_usage.csv'
  tmp_csv_path = BASE / 'daily_usage.tmp.csv'
  with sqlite3.connect(DB_PATH) as con:
    cur = con.cursor()
    rows = cur.execute("""
      SELECT
        f.snapshot_date,
        f.persistent_id,
        t.artist,
        t.album,
        t.name,
        t.album_artist,
        t.genre,
        t.composer,
        t.grouping,
        t.comments,
        t.year,
        t.bpm,
        t.rating,
        t.loved,
        t.disliked,
        t.duration_sec,
        t.track_number,
        t.track_count,
        t.disc_number,
        t.disc_count,
        t.kind,
        t.bit_rate,
        t.sample_rate,
        t.size_bytes,
        t.date_added,
        t.modification_date,
        t.compilation,
        t.explicit,
        t.cloud_status,
        f.play_count_total,
        f.skip_count_total,
        f.play_count_delta,
        f.skip_count_delta,
        f.last_played,
        f.last_skipped,
        CASE WHEN (f.play_count_delta + f.skip_count_delta) > 0
             THEN CAST(f.skip_count_delta AS REAL) / (f.play_count_delta + f.skip_count_delta)
             ELSE NULL
        END AS skip_rate
      FROM fact_usage f
      JOIN dim_track t USING(persistent_id)
      ORDER BY f.snapshot_date, t.artist, t.album, t.name
    """).fetchall()

  header = [
    "snapshot_date","persistent_id","artist","album","name","album_artist",
    "genre","composer","grouping","comments","year","bpm","rating","loved","disliked",
    "duration_sec","track_number","track_count","disc_number","disc_count","kind",
    "bit_rate","sample_rate","size_bytes","date_added","modification_date","compilation",
    "explicit","cloud_status","play_total","skip_total","play_delta","skip_delta",
    "last_played","last_skipped","skip_rate"
  ]
  with open(tmp_csv_path, 'w', newline='') as fh:
    w = csv.writer(fh)
    w.writerow(header)
    w.writerows(rows)
  os.replace(tmp_csv_path, csv_path)
  # --- end CSV export ---

  # Simple run log line (lands in stdout when run via launchd)
  now = datetime.datetime.now().isoformat(timespec='seconds')
  print(f"[{now}] snapshot={SNAPSHOT_DATE} CSV rebuilt, rows={len(rows)}")
  write_status("ok", f"tracks={len(data)} rows={len(rows)}")

def main():
  try:
    run_snapshot()
  except Exception as e:
    now = datetime.datetime.now().isoformat(timespec='seconds')
    print(f"[{now}] [ERROR] snapshot={SNAPSHOT_DATE} failed: {e}", file=sys.stderr)
    traceback.print_exc()
    write_status("error", str(e))
    sys.exit(1)

if __name__ == '__main__':
  main()
