"""The whole library, from Neo4j, for the guide's chat (D56).

The graph is built by `_tools/neo4j_library.py` in the research repo: every passage (:Segment) with its exact text,
in its work, by its author, and on the Chabad shelf linked to the 22 concepts. This module is what the website asks:

  GET /guide/lib/search?q=...&work=&author=     passages that have the words (Hebrew prefixes handled), best first
  GET /guide/lib/read?id=...&around=2           one passage whole, with the passages before and after it
  GET /guide/lib/related?id=...                 the passage's concepts, and other authors' passages on the same ones

The page offers these to Claude as tools; the tools run in the visitor's browser and call these routes. On only when
NEO4J_URI, NEO4J_USERNAME and NEO4J_PASSWORD are set on the host.
"""
from __future__ import annotations

import os
import re
import unicodedata

from fastapi import APIRouter, HTTPException

router = APIRouter()
from . import shelf as _shelf

URI = os.getenv("NEO4J_URI", "").strip()
NEO = bool(URI and os.getenv("NEO4J_PASSWORD", "").strip())
SHELF = (not NEO) and _shelf.AVAILABLE    # D91: without Neo4j, the deep shelf (SQLite full text) answers the same routes
ON = NEO or SHELF
if SHELF: _shelf.warm()
_drv = None


def driver():
    global _drv
    if not ON:
        raise HTTPException(404, "the library is not connected")
    if _drv is None:
        from neo4j import GraphDatabase
        _drv = GraphDatabase.driver(URI, auth=(os.getenv("NEO4J_USERNAME", "neo4j"), os.getenv("NEO4J_PASSWORD", "")),
                                    max_connection_lifetime=300)
    return _drv


def cy(cypher: str, **kw):
    return driver().execute_query(cypher, database_=os.getenv("NEO4J_DATABASE", "neo4j"), **kw).records


# ---------- the same normalisation the index was built with (_tools/library_search.py)
_T = {c: None for c in range(0x0591, 0x05C8)}
for c in (0x05BE, 0x05C0, 0x05C3, 0x05C6):
    _T[c] = " "
for c in range(0x0300, 0x0370):
    _T[c] = None
for a, b in zip("ךםןףץ", "כמנפצ"):
    _T[ord(a)] = b
for c in (0x05F3, 0x05F4, 0x200F, 0x200E, 0x00AD):
    _T[c] = None
_QA = re.compile(r"(?<=[א-ת])[\"'’”`]")


def norm(s: str) -> str:
    if not s:
        return ""
    if not s.isascii():
        s = _QA.sub("", unicodedata.normalize("NFKD", s).translate(_T))
    return s.lower()


PRE = ["ו", "ה", "ב", "כ", "ל", "מ", "ש", "ד", "וה", "וב", "וכ", "ול", "ומ", "וש", "וד", "שה", "שב",
       "של", "שמ", "מה", "לה", "בה", "כש", "וכש", "דה"]


def lucene(text: str, all_words: bool = True) -> str:
    text = norm(text.replace("“", '"').replace("”", '"'))
    esc = lambda w: re.sub(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)', r"\\\1", w)
    out = []
    for p in re.findall(r'"[^"]+"|\S+', text)[:12]:
        ws = re.findall(r"[\wא-ת]+", p.strip('"'))
        if not ws or (len(ws) == 1 and len(ws[0]) < 2):
            continue
        rest = (" " + " ".join(ws[1:])) if len(ws) > 1 else ""
        if re.match("[א-ת]", ws[0]) and len(ws[0]) >= 3:
            term = "(" + " OR ".join(f'"{esc(pre + ws[0] + rest)}"' for pre in [""] + PRE) + ")"
        else:
            term = f'"{esc(" ".join(ws))}"'
        out.append(("+" if all_words else "") + term)
    return " ".join(out)


SEARCH = """CALL db.index.fulltext.queryNodes('segment_text', $q) YIELD node AS s, score
  MATCH (s)-[:IN_WORK]->(w:Work)<-[:WROTE]-(a:Author)
  WHERE ($work = '' OR toLower(w.title) CONTAINS toLower($work) OR coalesce(w.heTitle, '') CONTAINS $work)
    AND ($author = '' OR toLower(a.name) CONTAINS toLower($author))
  RETURN s.id AS id, s.ref AS ref, w.title AS work, a.name AS author, coalesce(s.text, s.plain) AS text, score
  ORDER BY score DESC LIMIT $n"""


@router.get("/guide/lib/search")
def lib_search(q: str = "", work: str = "", author: str = "", n: int = 8):
    words = q.strip()[:300]
    if not words:
        raise HTTPException(400, "q is required")
    n = max(1, min(n, 12))
    if SHELF:
        if not _shelf.ready():
            return {"query": words, "results": [], "note": "The library is still opening (about a minute after the site wakes). Answer from what you have, and try again next turn."}
        return {"query": words, "results": _shelf.search(words, work.strip()[:100], author.strip()[:100], n)}
    rows = []
    for all_words in (True, False):
        lq = lucene(words, all_words)
        if not lq:
            break
        rows = cy(SEARCH, q=lq, work=work.strip()[:100], author=author.strip()[:100], n=n * 3)
        if rows:
            break
    seen, out = set(), []
    for r in rows:
        k = norm(r["text"])[:120]
        if k in seen:
            continue
        seen.add(k)
        t = r["text"]
        out.append({"id": r["id"], "ref": r["ref"], "work": r["work"], "author": r["author"],
                    "text": t[:700] + ("…" if len(t) > 700 else "")})
        if len(out) >= n:
            break
    return {"query": words, "results": out}


@router.get("/guide/lib/read")
def lib_read(id: str, around: int = 1):
    around = max(0, min(around, 4))
    if SHELF:
        r = _shelf.read(id, around)
        if not r: raise HTTPException(404, "no passage with that id")
        return r
    me = cy("""MATCH (s:Segment {id: $id})-[:IN_WORK]->(w:Work)<-[:WROTE]-(a:Author)
        OPTIONAL MATCH (s)-[:PRIMARY|TAGGED]->(c:Concept)
        RETURN s.work AS wk, s.seq AS seq, s.ref AS ref, coalesce(s.text, s.plain) AS text, s.en AS en,
               w.title AS work, a.name AS author, collect(DISTINCT c.name) AS concepts""", id=id[:40])
    if not me:
        raise HTTPException(404, "no passage with that id")
    m = me[0]
    near = cy("""MATCH (s:Segment) WHERE s.work = $wk AND s.seq >= $a AND s.seq <= $b
        RETURN s.id AS id, s.seq AS seq, s.ref AS ref, coalesce(s.text, s.plain) AS text ORDER BY s.seq""",
                          wk=m["wk"], a=(m["seq"] or 0) - around, b=(m["seq"] or 0) + around)
    cut = lambda t, k: t if len(t) <= k else t[:k] + "…"
    return {"id": id, "ref": m["ref"], "work": m["work"], "author": m["author"], "concepts": m["concepts"],
            "text": cut(m["text"], 12000), "translation": cut(m["en"] or "", 6000),
            "before": [{"id": r["id"], "ref": r["ref"], "text": cut(r["text"], 3000)} for r in near if r["seq"] < m["seq"]],
            "after": [{"id": r["id"], "ref": r["ref"], "text": cut(r["text"], 3000)} for r in near if r["seq"] > m["seq"]]}


@router.get("/guide/lib/related")
def lib_related(id: str, n: int = 6):
    n = max(1, min(n, 10))
    if SHELF:
        return {"id": id, "concepts": [], "related": [], "note": "Related passages need the concept graph; search the library with the passage's key words instead."}
    rows = cy("""MATCH (s:Segment {id: $id})-[:PRIMARY|TAGGED]->(c:Concept)
        WITH s, collect(c) AS cs
        UNWIND cs AS c
        MATCH (o:Segment)-[:PRIMARY]->(c)
        WHERE o.work <> s.work
        WITH s, cs, o, count(c) AS shared
        ORDER BY shared DESC, o.words DESC
        WITH s, cs, o.work AS wk, collect({o: o, shared: shared})[0] AS best
        ORDER BY best.shared DESC LIMIT $n
        WITH cs, best.o AS o, best.shared AS shared
        MATCH (o)-[:IN_WORK]->(w:Work)<-[:WROTE]-(a:Author)
        RETURN [x IN cs | x.name] AS concepts, o.id AS id, o.ref AS ref, w.title AS work, a.name AS author,
               coalesce(o.text, o.plain) AS text, shared""", id=id[:40], n=n)
    if not rows:
        return {"id": id, "concepts": [], "related": [], "note": "This passage has no concept tags; search instead."}
    return {"id": id, "concepts": rows[0]["concepts"],
            "related": [{"id": r["id"], "ref": r["ref"], "work": r["work"], "author": r["author"], "shared": r["shared"],
                         "text": r["text"][:600] + ("…" if len(r["text"]) > 600 else "")} for r in rows]}


@router.get("/guide/lib/health")
def lib_health():
    if not ON:
        return {"connected": False}
    if SHELF:
        return {"connected": True, "engine": "shelf", "ready": _shelf.ready(), "works": _shelf.works()}
    n = cy("MATCH (s:Segment) RETURN count(s) AS n")[0]["n"]
    return {"connected": True, "passages": n}
