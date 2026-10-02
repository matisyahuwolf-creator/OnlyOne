"""The deep shelf (D91): the Rebbeim's works themselves, searchable without a database server.

The site carries the core Chassidic works as gzipped text in `shelf/` (one passage per line, "ref ⟶ text"). On first
use, or at build time (`python -m app.shelf`), they are indexed into a local SQLite full-text index, `shelf.db`. The
library routes in `library.py` use this when Neo4j is not configured, with the same answers: search, read, related.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
import sqlite3
import threading
from pathlib import Path

log = logging.getLogger("shelf")
SITE = Path(__file__).resolve().parents[1]
DIR = Path(os.getenv("GUIDE_SHELF_DIR") or (SITE / "shelf"))
DB = Path(os.getenv("GUIDE_SHELF_DB") or (SITE / "shelf.db"))
CHUNK = 1400          # long lines are cut into pieces about this long, at a space
NIKUD = re.compile(r"[֑-ׇ]")
PREFIX = ["ו", "ה", "ב", "ל", "מ", "ש", "כ", "וה", "וב", "ול", "ומ", "וש", "שה", "שב", "מה", "בה", "לה", "כש", "וכ", "דה", "ד"]

AVAILABLE = DIR.is_dir() and any(DIR.glob("*.txt.gz"))
_ready = threading.Event()
_lock = threading.Lock()


def plain(s: str) -> str:
    s = NIKUD.sub("", s or "").replace("״", '"').replace("׳", "'")
    return re.sub(r"[^\w\"' ]+", " ", s).lower()


def _meta() -> dict:
    f = DIR / "works.json"
    return json.load(open(f, encoding="utf-8")) if f.exists() else {}


def build(force: bool = False) -> int:
    """Index every shelf file into shelf.db. Returns the number of passages."""
    with _lock:
        if DB.exists() and not force:
            _ready.set()
            return -1
        tmp = DB.with_suffix(".tmp")
        if tmp.exists(): tmp.unlink()
        con = sqlite3.connect(tmp)
        con.executescript("""
            CREATE TABLE p (id INTEGER PRIMARY KEY, work INTEGER, seq INTEGER, ref TEXT, text TEXT);
            CREATE TABLE w (id INTEGER PRIMARY KEY, title TEXT, author TEXT, ocr INTEGER);
            CREATE VIRTUAL TABLE f USING fts5(body, content='', tokenize='unicode61 remove_diacritics 2');""")
        meta, n = _meta(), 0
        for wi, path in enumerate(sorted(DIR.glob("*.txt.gz"))):
            m = meta.get(path.name, {})
            con.execute("INSERT INTO w VALUES (?,?,?,?)", (wi, m.get("title") or path.name[:-7], m.get("author", ""), 1 if m.get("ocr") else 0))
            seq, rows = 0, []
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line: continue
                    ref, _, text = line.partition(" ⟶ ")
                    if not text: ref, text = "", line
                    if len(re.findall(r"[א-ת]", text)) < 12: continue
                    pieces = [text] if len(text) <= CHUNK * 1.4 else _cut(text)
                    for k, t in enumerate(pieces):
                        seq += 1; n += 1
                        rows.append((n, wi, seq, ref + (f" (part {k + 1} of {len(pieces)})" if len(pieces) > 1 else ""), t))
            con.executemany("INSERT INTO p VALUES (?,?,?,?,?)", rows)
            con.executemany("INSERT INTO f(rowid, body) VALUES (?,?)", [(r[0], plain(r[4])) for r in rows])
            con.commit()
        con.execute("CREATE INDEX pw ON p(work, seq)")
        con.commit(); con.close()
        os.replace(tmp, DB)
        _ready.set()
        log.info("shelf: indexed %d passages", n)
        return n


def _cut(text: str) -> list[str]:
    out, i = [], 0
    while i < len(text):
        j = min(len(text), i + CHUNK)
        if j < len(text):
            k = text.rfind(" ", i + CHUNK // 2, j)
            j = k if k > 0 else j
        out.append(text[i:j].strip()); i = j
    return [x for x in out if x]


def warm() -> None:
    """Make sure the index exists, building it in the background the first time."""
    if not AVAILABLE or _ready.is_set(): return
    if DB.exists(): _ready.set(); return
    threading.Thread(target=lambda: build(), daemon=True).start()


def ready() -> bool:
    if not _ready.is_set() and DB.exists() and not _lock.locked(): _ready.set()
    return _ready.is_set()


def _con():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def _fts(q: str, all_words: bool) -> str:
    phrases = re.findall(r'"([^"]+)"', q)
    rest = re.sub(r'"[^"]+"', " ", q)
    parts = [ '"' + " ".join(plain(p).split()) + '"' for p in phrases if plain(p).split()]
    for w in plain(rest).split():
        w = w.strip("\"'")
        if len(w) < 2: continue
        forms = {w} | {p + w for p in PREFIX if len(w) >= 2}
        for p in PREFIX:
            if w.startswith(p) and len(w) - len(p) >= 3: forms.add(w[len(p):])
        parts.append("(" + " OR ".join('"' + f.replace('"', '') + '"' for f in sorted(forms)) + ")")
    return (" AND " if all_words else " OR ").join(parts)


def search(q: str, work: str = "", author: str = "", n: int = 8) -> list[dict]:
    if not ready(): return []
    con = _con(); out = []
    try:
        for all_words in (True, False):
            fq = _fts(q, all_words)
            if not fq: break
            sql = """SELECT p.id, p.ref, p.text, w.title, w.author, w.ocr, bm25(f) AS s FROM f JOIN p ON p.id = f.rowid JOIN w ON w.id = p.work
                     WHERE f MATCH ? AND (? = '' OR w.title LIKE ?) AND (? = '' OR w.author LIKE ?) ORDER BY s + w.ocr * 2 LIMIT ?"""
            rows = con.execute(sql, (fq, work, f"%{work}%", author, f"%{author}%", n * 3)).fetchall()
            if rows: break
        seen = set()
        for r in rows:
            k = plain(r["text"])[:100]
            if k in seen: continue
            seen.add(k)
            t = r["text"]
            out.append({"id": f"s{r['id']}", "ref": r["ref"], "work": r["title"], "author": r["author"], "scan": bool(r["ocr"]),
                        "text": t[:700] + ("…" if len(t) > 700 else "")})
            if len(out) >= n: break
    finally:
        con.close()
    return out


def read(pid: str, around: int = 1) -> dict | None:
    if not ready(): return None
    m = re.fullmatch(r"s(\d+)", pid.strip())
    if not m: return None
    con = _con()
    try:
        r = con.execute("SELECT p.*, w.title, w.author, w.ocr FROM p JOIN w ON w.id = p.work WHERE p.id = ?", (int(m.group(1)),)).fetchone()
        if not r: return None
        near = con.execute("SELECT id, ref, text, seq FROM p WHERE work = ? AND seq BETWEEN ? AND ? ORDER BY seq", (r["work"], r["seq"] - around, r["seq"] + around)).fetchall()
    finally:
        con.close()
    cut = lambda t, k: t if len(t) <= k else t[:k] + "…"
    return {"id": pid, "ref": r["ref"], "work": r["title"], "author": r["author"], "scan": bool(r["ocr"]), "text": cut(r["text"], 12000),
            "before": [{"id": f"s{x['id']}", "ref": x["ref"], "text": cut(x["text"], 3000)} for x in near if x["seq"] < r["seq"]],
            "after": [{"id": f"s{x['id']}", "ref": x["ref"], "text": cut(x["text"], 3000)} for x in near if x["seq"] > r["seq"]]}


def works() -> list[dict]:
    if not ready(): return []
    con = _con()
    try:
        return [{"work": r["title"], "author": r["author"], "passages": r["n"]} for r in
                con.execute("SELECT w.title, w.author, count(p.id) AS n FROM w JOIN p ON p.work = w.id GROUP BY w.id ORDER BY w.id")]
    finally:
        con.close()


if __name__ == "__main__":
    import sys, time
    t = time.time(); n = build(force="--force" in sys.argv)
    print(f"shelf: {n} passages in {time.time() - t:.0f}s -> {DB} ({DB.stat().st_size // 1048576} MB)")
