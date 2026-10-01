"""The guide, served by this app at /guide.

The guide is the page built by `Tanya Guided Meditation/_build/build_engine.py`
(universal/engine.html): the conversation under the constitution, the medicine
map, the root -> meditation suggestion, the Chassidus maps, and personal guided
meditations read aloud. As a claude.ai artifact it asks Claude through the
artifact runtime; here a small shim gives it the same `sample` call, answered
by this server with this app's Anthropic key.

Two ways to open it:
- With an access code (the default): the same passphrase as /chat.
- Public (GUIDE_PUBLIC=1): anyone with the link, no code. Then the guards below
  carry the cost: each visitor (an anonymous cookie), each network (IP) and the
  whole site have hourly and daily ceilings; only the page's own kinds of call
  are answered; the unchanging front of every prompt is cached so repeat calls
  pay a tenth for it. Nothing a person writes is stored here: the conversation
  lives in their browser, and this server keeps only counts and token totals.
"""
from __future__ import annotations

import gzip
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

import anthropic
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

from . import config

log = logging.getLogger("mashpia.guide")
router = APIRouter()

_IN_REPO = Path(__file__).resolve().parents[2] / "Tanya Guided Meditation" / "universal" / "engine.html"
# In the research repo the page is built beside this app; in a packed site (scripts/pack_guide_site.py) it sits next to app/.
PAGE = Path(os.getenv("GUIDE_PAGE") or (_IN_REPO if _IN_REPO.exists() else Path(__file__).resolve().parents[1] / "engine.html"))
QUICK_MODEL = os.getenv("GUIDE_QUICK_MODEL", "claude-haiku-4-5")
MAX_INPUT = 200_000  # characters; the page keeps each call well under this


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _num(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


PUBLIC = _flag("GUIDE_PUBLIC")
# Ceilings, counted in main replies (the model that writes replies and meditations). The small
# listening and map calls ride along and are capped at three times these. 0 turns a ceiling off.
PER_HOUR = _num("GUIDE_REPLIES_PER_HOUR", 30)        # one visitor, one hour
PER_DAY = _num("GUIDE_REPLIES_PER_DAY", 100)         # one visitor, one day
NET_FACTOR = _num("GUIDE_NETWORK_FACTOR", 4)         # one network (a home, an office) = this many visitors
SITE_PER_DAY = _num("GUIDE_SITE_REPLIES_PER_DAY", 2000)  # everyone, one day: the cost ceiling
TRUST_PROXY = os.getenv("GUIDE_TRUST_PROXY", "1").strip().lower() not in ("0", "false", "no", "off")
OWNER_CODE = os.getenv("GUIDE_OWNER_CODE", "").strip()
DB = Path(os.getenv("GUIDE_DB") or Path(__file__).resolve().parent.parent / "guide_usage.db")
# Saved conversations (D54): the owner reads them to improve the guide. On whenever DATABASE_URL is set (a Postgres
# that survives restarts, e.g. a free Neon database), or GUIDE_LOG=1 (then a local file, which a free host may wipe).
# The page tells visitors plainly that chats are saved. No IP addresses are kept; old chats are deleted after
# GUIDE_LOG_DAYS days.
DB_URL = os.getenv("DATABASE_URL", "").strip()
LOG_ON = bool(DB_URL) or _flag("GUIDE_LOG")
LOG_DAYS = _num("GUIDE_LOG_DAYS", 90)
LOG_NEW_PER_DAY = 40      # new conversations one visitor may start in a day

# The page's kinds of call, by how their prompts begin (public mode answers nothing else).
HEADS = ("You keep a small, honest profile of a person",   # listening
         "You review one message from a guide",             # reply check
         "You review a guided meditation",                  # meditation check
         "You translate the words of a web page")           # the page's buttons, into the visitor's language

# Plain mode (D49): the page sends the conversation as turns, the first opening with this note, and offers two
# page tools over the research graph. The tools run in the visitor's browser; this server only relays the rounds.
PLAIN_HEAD = "[A note from the page, not from the person: You're Claude, chatting with someone on a page called Only One."
KG_TOOLS = [
    {"name": "search_research",
     "description": "Searches the Only One research library on Chassidus (Chabad teachings on the unity of G-d): core truths with checked Hebrew sources, what people commonly carry and what the sources say about it, states of consciousness and the movements between them, healing methods, meditations and techniques, perception shifts, and explanations of why a person is held by G-d. Returns up to 8 matches, each with an id, type, title and gist. Use a few plain English keywords.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string", "description": "plain English keywords"}}, "required": ["query"]}},
    {"name": "open_research_item",
     "description": "Opens one item from search_research by its id: its full content (with Hebrew source quotes and references when it has them) and the ids and titles of linked items, which you can open too.",
     "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "read_tanya",
     "description": "Opens the full Hebrew text of any chapter or letter of the Tanya, paragraph by paragraph, e.g. \"Likkutei Amarim 32\", \"Iggeret HaKodesh 22\", \"Shaar HaYichud VehaEmunah 1\", \"Iggeret HaTeshuvah 7\", \"Kuntres Acharon 4\". Use it whenever someone asks about a specific chapter or letter, and quote only from what it returns.",
     "input_schema": {"type": "object", "properties": {"where": {"type": "string"}, "from": {"type": "integer", "description": "paragraph to start from, for long letters"}}, "required": ["where"]}},
    {"name": "search_tanya",
     "description": "Searches the full Hebrew text of the Tanya for words or a phrase (write them in Hebrew, without vowels) and returns up to 8 paragraphs with their references.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
]
from . import library as _lib
if _lib.ON:   # D56: the whole library, from Neo4j; the tools run in the browser and call /guide/lib/*
    KG_TOOLS += [
        {"name": "search_library",
         "description": "Searches the whole library: every Chassidic work (the Baal Shem Tov and his students, all the Chabad Rebbeim and their books), the Sefaria shelves (Tanakh, Midrash, Kabbalah, Musar, Jewish thought), the teachers' books and talks (Bilvavi, Rav Morgenstern, Rav Weinberger, Rav Asher Freund, Rav Joey Rosenfeld and others). Returns up to 8 passages with id, reference, work, author and text. Write the words in Hebrew for Hebrew sources (vowels not needed; prefixes like ו, ה, ב are handled); put an exact phrase in quotes. Optional work and author narrow it.",
         "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "work": {"type": "string"}, "author": {"type": "string"}}, "required": ["query"]}},
        {"name": "read_passage",
         "description": "Opens one passage from search_library by its id: the whole exact text, a translation when the source has one, its concepts, and the passages right before and after it. Quote only from what this returns.",
         "input_schema": {"type": "object", "properties": {"id": {"type": "string"}, "around": {"type": "integer", "description": "how many passages before and after (0-4, default 1)"}}, "required": ["id"]}},
        {"name": "related_passages",
         "description": "Follows the graph from one passage: its concepts (of the 22 Chassidic pillars), and passages by other authors and works on the same concepts.",
         "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    ]
KG_NAMES = {t["name"] for t in KG_TOOLS}
MAX_ROUNDS = 6   # tool rounds in one reply; the last one must answer
BLOCK_TYPES = {"text", "tool_use", "tool_result", "thinking", "redacted_thinking", "fallback"}

SHIM = r"""<script>
// Runs before the page: gives it the artifact runtime's `sample` call, answered by /guide/sample.
(function(){
  const PUBLIC = __PUBLIC__;
  window.OO_SITE = true;   // served here, not inside claude.ai: the page may translate its words at once
  window.OO_TTS = __TTS__;   // a real voice (ElevenLabs) for reading meditations aloud, when configured
  window.OO_FEEDBACK_EMAIL = __FEEDBACK__;
  window.OO_LOG = __LOG__;
  window.OO_LIB = __LIB__;   // the whole library is connected (Neo4j), searchable from the chat   // conversations are saved for the owner (the page says so under the chat)   // "Send this chat" opens the person's email to this address (GUIDE_FEEDBACK_EMAIL); empty: share or copy
  let code = PUBLIC ? '' : (localStorage.getItem('mashpia_code') || '');
  async function ok(c){ try{ return (await fetch('/guide/check',{headers:{'X-Chat-Code':c}})).ok; }catch(e){ return false; } }
  async function ensure(){
    if(PUBLIC) return;
    while(!(code && await ok(code))){
      const c = prompt('Access code for this guide:'); if(c===null) throw {code:'not_granted', message:'no access code'};
      code = c.trim(); localStorage.setItem('mashpia_code', code);
    }
  }
  async function call(input, opts, json){
    opts = opts || {}; await ensure();
    const stream = !!opts.onText && !json;
    const r = await fetch('/guide/sample', {method:'POST', signal: opts.signal, credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-Chat-Code':code},
      body: JSON.stringify({input, tier: opts.modelTier || 'default', json: !!json, stream})});
    if(r.status===401){ localStorage.removeItem('mashpia_code'); code=''; throw {code:'not_granted', message:'bad access code'}; }
    if(r.status===429){ let d={}; try{ d = (await r.json()).detail || {}; }catch(e){} throw {code:'rate_limited', reason: d.reason || '', message: 'slow down'}; }
    if(!r.ok) throw {code:'error', message:'HTTP '+r.status};
    if(!stream){ const d = await r.json(); return d.text || ''; }
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf='', text='';
    for(;;){ const {value, done} = await rd.read(); if(done) break;
      buf += dec.decode(value, {stream:true}); const parts = buf.split('\n\n'); buf = parts.pop();
      for(const p of parts){ const ev=(p.match(/^event: (.+)$/m)||[])[1]; const dm=(p.match(/^data: (.*)$/m)||[])[1];
        if(ev==='text'){ const delta = JSON.parse(dm); text += delta; opts.onText({text, delta}); }
        else if(ev==='error'){ throw {code:'error', message: JSON.parse(dm), text}; } } }
    return text;
  }
  function parseJSON(t){
    const s = t.replace(/^```(?:json)?\s*|\s*```$/g,'').trim();
    try{ return JSON.parse(s); }catch(e){}
    const a = s.indexOf('{'), b = s.lastIndexOf('}');
    if(a>=0 && b>a){ try{ return JSON.parse(s.slice(a,b+1)); }catch(e){} }
    throw {code:'error', message:'the reply was not JSON'};
  }
  // One round with the page tools: streams the text, then hands back the round's content blocks and why it stopped.
  async function round(msgs, opts, n, last){
    await ensure();
    const r = await fetch('/guide/sample', {method:'POST', signal: opts.signal, credentials:'same-origin',
      headers:{'Content-Type':'application/json','X-Chat-Code':code},
      body: JSON.stringify({input: msgs, tier: opts.modelTier || 'default', tools: true, round: n, last, stream: true})});
    if(r.status===401){ localStorage.removeItem('mashpia_code'); code=''; throw {code:'not_granted', message:'bad access code'}; }
    if(r.status===429){ let d={}; try{ d = (await r.json()).detail || {}; }catch(e){} throw {code:'rate_limited', reason: d.reason || '', message: 'slow down'}; }
    if(!r.ok) throw {code:'error', message:'HTTP '+r.status};
    const rd = r.body.getReader(), dec = new TextDecoder(); let buf='', fin=null;
    for(;;){ const {value, done} = await rd.read(); if(done) break;
      buf += dec.decode(value, {stream:true}); const parts = buf.split('\n\n'); buf = parts.pop();
      for(const p of parts){ const ev=(p.match(/^event: (.+)$/m)||[])[1]; const dm=(p.match(/^data: (.*)$/m)||[])[1];
        if(ev==='text') opts.__delta(JSON.parse(dm));
        else if(ev==='final') fin = JSON.parse(dm);
        else if(ev==='error') throw {code:'error', message: JSON.parse(dm)}; } }
    if(!fin) throw {code:'error', message:'the reply was cut off'};
    return fin;
  }
  async function withTools(input, opts){
    const msgs = (typeof input === 'string' ? [{role:'user', content: input}] : input).map(m=>({role: m.role, content: m.content}));
    let text = '';
    opts.__delta = d => { text += d; opts.onText && opts.onText({text, delta: d}); };
    for(let n = 0; n < __ROUNDS__; n++){
      const fin = await round(msgs, opts, n, n === __ROUNDS__ - 1);
      if(fin.stop_reason === 'refusal' && !text) text = "I'm sorry, I can't help with that one.";
      if(fin.stop_reason !== 'tool_use') return text;
      msgs.push({role:'assistant', content: fin.content});
      const uses = fin.content.filter(b => b.type === 'tool_use');
      const results = await Promise.all(uses.map(async u => {
        const t = opts.tools.find(x => x.name === u.name);
        try{ if(!t) throw new Error('no such tool'); const out = await t.execute(u.input || {}, {signal: (opts.signal || new AbortController().signal)});
             return {type:'tool_result', tool_use_id: u.id, content: typeof out === 'string' ? out : JSON.stringify(out)}; }
        catch(e){ return {type:'tool_result', tool_use_id: u.id, content: 'Error: ' + (e && e.message || e), is_error: true}; }
      }));
      msgs.push({role:'user', content: results});
      if(text && !/\n\n$/.test(text)) opts.__delta('\n\n');
    }
    return text;
  }
  const sample = async (input, opts) => { opts = opts || {};
    const text = (opts.tools && opts.tools.length) ? await withTools(input, opts) : await call(input, opts, false);
    return { text, truncated: false, modelTierApplied: opts.modelTier || 'default' }; };
  sample.json = async (input, opts) => parseJSON(await call(input, opts, true));
  sample.limits = async () => ({ images: false, tools: { maxCount: 4 } });
  window.claude = { use: async name => name === 'sample' ? sample : null };
  if(window.OO_TTS) window.OO_tts = async (text, prev) => {   // one paragraph, read by the real voice
    await ensure();
    const r = await fetch('/guide/tts', {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json','X-Chat-Code':code},
      body: JSON.stringify({text, prev: prev || ''})});
    if(!r.ok) throw new Error('tts '+r.status);
    return await r.blob();
  };
})();
</script>"""


# ---------- the page, and the unchanging text its prompts begin with

_page_cache: dict = {"mtime": None, "html": "", "constitution": "", "gz": {}}


def _page() -> tuple[str, str]:
    """The built page and its constitution, re-read when the page is rebuilt (no restart needed)."""
    if not PAGE.exists():
        raise HTTPException(404, f"guide page not found at {PAGE}; build it with _build/build_engine.py or set GUIDE_PAGE")
    m = PAGE.stat().st_mtime
    if _page_cache["mtime"] != m:
        html = PAGE.read_text(encoding="utf-8")
        cons = ""
        a = html.find('<script id="bundle" type="application/json">')
        if a >= 0:
            a = html.index(">", a) + 1
            try:
                cons = json.loads(html[a:html.index("</script>", a)]).get("constitution", "")
            except (ValueError, json.JSONDecodeError):
                log.warning("could not read the constitution from the page bundle; prompt caching is off")
        _page_cache.update(mtime=m, html=html, constitution=cons, gz={})
    return _page_cache["html"], _page_cache["constitution"]


def _guide_shaped(text: str, constitution: str) -> bool:
    return bool(constitution and text.startswith(constitution)) or text.startswith(HEADS)


def _blocks(text: str, constitution: str):
    """Split a prompt so its unchanging front is cached: the constitution; for a reply, the page's
    own instructions after it; for a listening call, the rules and the menu. The model reads the
    same text either way."""
    cuts = []
    if constitution and text.startswith(constitution):
        cuts.append(len(constitution))
        i = text.find("\nRISK THIS TURN:", len(constitution))
        if i > len(constitution):
            cuts.append(i)
    elif text.startswith(HEADS[0]):
        i = text.find("\n\nCURRENT LENS:")
        if i > 0:
            cuts.append(i)
    if not cuts:
        return text
    blocks, prev = [], 0
    for k in cuts:
        if k > prev:
            blocks.append({"type": "text", "text": text[prev:k], "cache_control": {"type": "ephemeral"}})
            prev = k
    if prev < len(text):
        blocks.append({"type": "text", "text": text[prev:]})
    return blocks


# ---------- who is asking, and how much they have asked

_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(DB, timeout=10)
    c.execute("""CREATE TABLE IF NOT EXISTS guide_usage (
                   id INTEGER PRIMARY KEY, ts REAL NOT NULL, visitor TEXT, network TEXT, tier TEXT NOT NULL,
                   input INTEGER DEFAULT 0, output INTEGER DEFAULT 0, cache_read INTEGER DEFAULT 0,
                   cache_write INTEGER DEFAULT 0)""")
    c.execute("CREATE INDEX IF NOT EXISTS guide_usage_ts ON guide_usage(ts)")
    return c


def _network(request: Request) -> str:
    if TRUST_PROXY:
        fwd = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0]
        if fwd.strip():
            return fwd.strip()
    return request.client.host if request.client else "unknown"


def _is_owner(request: Request) -> bool:
    """The owner's browser carries a cookie set by /guide/owner?code=...; it is never limited per visitor."""
    c = request.cookies.get("oo_owner", "")
    return bool(OWNER_CODE) and bool(c) and hmac.compare_digest(c, OWNER_CODE)


@router.get("/guide/owner")
def guide_owner(code: str = "") -> Response:
    """Opens the guide for the owner with no per-visitor limits (the site's daily cost ceiling still holds)."""
    _owner(code)
    r = RedirectResponse("/guide", status_code=303)
    r.set_cookie("oo_owner", code, max_age=86400 * 365, httponly=True, secure=True, samesite="lax")
    return r


def _admit(visitor: str, network: str, tier: str, owner: bool = False) -> int:
    """Checks the ceilings and reserves this call. Returns the row id; raises 429 with a reason."""
    now = time.time()
    k = {"default": 1, "round": MAX_ROUNDS}.get(tier, 3)   # the small calls get three times the room; tool rounds, one reply's worth
    with _lock, _db() as c:
        c.execute("DELETE FROM guide_usage WHERE ts < ?", (now - 86400 * 30,))

        def count(where: str, args: tuple, since: float) -> int:
            return c.execute(f"SELECT count(*) FROM guide_usage WHERE tier=? AND ts>? {where}",
                             (tier, since) + args).fetchone()[0]

        if PUBLIC and not owner:
            if visitor and PER_HOUR and count("AND visitor=?", (visitor,), now - 3600) >= PER_HOUR * k:
                raise HTTPException(429, detail={"reason": "hour"})
            if visitor and PER_DAY and count("AND visitor=?", (visitor,), now - 86400) >= PER_DAY * k:
                raise HTTPException(429, detail={"reason": "day"})
            if PER_HOUR and count("AND network=?", (network,), now - 3600) >= PER_HOUR * k * NET_FACTOR:
                raise HTTPException(429, detail={"reason": "network"})
            if PER_DAY and count("AND network=?", (network,), now - 86400) >= PER_DAY * k * NET_FACTOR:
                raise HTTPException(429, detail={"reason": "network"})
        if SITE_PER_DAY and count("", (), now - 86400) >= SITE_PER_DAY * k:
            raise HTTPException(429, detail={"reason": "site"})
        return c.execute("INSERT INTO guide_usage (ts, visitor, network, tier) VALUES (?,?,?,?)",
                         (now, visitor, network, tier)).lastrowid


def _record(row: int, usage) -> None:
    if not usage:
        return
    with _lock, _db() as c:
        c.execute("UPDATE guide_usage SET input=?, output=?, cache_read=?, cache_write=? WHERE id=?",
                  (getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0,
                   getattr(usage, "cache_read_input_tokens", 0) or 0,
                   getattr(usage, "cache_creation_input_tokens", 0) or 0, row))


def _gate(code: str) -> None:
    if PUBLIC:
        return
    if not config.CHAT_ACCESS_CODE:
        raise HTTPException(503, "CHAT_ACCESS_CODE is not set; /guide is disabled. Set it, or GUIDE_PUBLIC=1.")
    if not hmac.compare_digest(code, config.CHAT_ACCESS_CODE):
        raise HTTPException(401, "bad access code")


# ---------- routes

_SRC_CACHE: dict[str, bytes] = {}

@router.get("/guide/src/{name}")
def guide_source(name: str, request: Request) -> Response:
    """The direct sources (D89): the book passages behind the source numbers, src/p0.json .. pN.json next to the page."""
    import re as _re
    if not _re.fullmatch(r"p\d{1,3}\.json", name):
        raise HTTPException(404, "not found")
    f = PAGE.parent / "src" / name
    if not f.is_file():
        raise HTTPException(404, "not found")
    gz = _SRC_CACHE.get(name) or _SRC_CACHE.setdefault(name, gzip.compress(f.read_bytes(), 6))
    hdr = {"Cache-Control": "public, max-age=86400", "Vary": "Accept-Encoding"}
    if "gzip" in request.headers.get("accept-encoding", ""):
        return Response(gz, media_type="application/json; charset=utf-8", headers=hdr | {"Content-Encoding": "gzip"})
    return Response(f.read_bytes(), media_type="application/json; charset=utf-8", headers=hdr)

@router.get("/guide", response_class=HTMLResponse)
def guide_page(request: Request) -> HTMLResponse:
    html, _ = _page()
    head = '<!doctype html><html lang="en"><head><meta charset="utf-8">' \
           '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
    doc = head + SHIM.replace("__ROUNDS__", str(MAX_ROUNDS)).replace("__PUBLIC__", "true" if PUBLIC else "false").replace("__LOG__", "true" if LOG_ON else "false").replace("__LIB__", "true" if _lib.ON else "false").replace("__FEEDBACK__", json.dumps(os.getenv("GUIDE_FEEDBACK_EMAIL", "").strip()).replace("<", "\\u003c")).replace("__TTS__", "true" if (os.getenv("ELEVENLABS_API_KEY", "").strip() and os.getenv("ELEVENLABS_VOICE_ID", "").strip()) else "false") + "</head><body>" + html + "</body></html>"
    if "gzip" in request.headers.get("accept-encoding", ""):
        # the page is ~2 MB of text; compressed it is about a fifth, which matters on a slow phone
        gz = _page_cache["gz"].get(PUBLIC) or _page_cache["gz"].setdefault(PUBLIC, gzip.compress(doc.encode("utf-8"), 6))
        r = Response(gz, media_type="text/html; charset=utf-8", headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
    else:
        r = HTMLResponse(doc)
    if not request.cookies.get("oo_v"):
        r.set_cookie("oo_v", secrets.token_hex(12), max_age=365 * 86400, httponly=True, samesite="lax",
                     secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return r


@router.get("/guide/check")
def guide_check(x_chat_code: str = Header(default="")) -> JSONResponse:
    """Lets the page test an access code before its first call (always fine in public mode)."""
    _gate(x_chat_code)
    return JSONResponse({"ok": True, "public": PUBLIC})


def _plain_turns(inp) -> list[dict] | None:
    """The plain-mode conversation, checked: user and assistant turns only, known block types only, the page's
    own opening note first, and a bounded size. Returns None when the input is not plain-mode turns."""
    if not (isinstance(inp, list) and inp and isinstance(inp[0], dict) and inp[0].get("role") == "user"):
        return None
    first = inp[0].get("content")
    head = first if isinstance(first, str) else next((b.get("text", "") for b in first if isinstance(b, dict) and b.get("type") == "text"), "") if isinstance(first, list) else ""
    if not head.startswith(PLAIN_HEAD):
        return None
    if len(json.dumps(inp)) > MAX_INPUT * 3:
        raise HTTPException(413, "the conversation is too long; start a new one")
    out = []
    for m in inp[-80:] if len(inp) > 80 else inp:
        role, content = m.get("role"), m.get("content")
        if role not in ("user", "assistant"):
            raise HTTPException(400, "bad turn")
        if isinstance(content, str):
            out.append({"role": role, "content": content[:MAX_INPUT]})
            continue
        if not isinstance(content, list) or any(not isinstance(b, dict) or b.get("type") not in BLOCK_TYPES for b in content):
            raise HTTPException(400, "bad content")
        if any(b.get("type") == "tool_use" and b.get("name") not in KG_NAMES for b in content):
            raise HTTPException(400, "unknown tool")
        out.append({"role": role, "content": _echo(content) if role == "assistant" else content})
    if out[0]["role"] != "user":
        raise HTTPException(400, "the conversation must start with the person")
    return out


def _echo(blocks: list[dict]) -> list[dict]:
    """An assistant turn sent back as it came, except after a mid-output fallback: the declined model's thinking
    and tool calls before the last fallback marker are left out, as the API asks."""
    idx = [i for i, b in enumerate(blocks) if b.get("type") == "fallback"]
    if not idx:
        return blocks
    cut = idx[-1]
    return [b for i, b in enumerate(blocks) if i > cut or b.get("type") == "text"]


def _messages(inp, constitution: str) -> list[dict]:
    if isinstance(inp, str):
        text = inp[:MAX_INPUT]
        if PUBLIC and not _guide_shaped(text, constitution):
            raise HTTPException(400, "this endpoint answers only the guide page")
        return [{"role": "user", "content": _blocks(text, constitution)}]
    plain = _plain_turns(inp)
    if plain:
        return plain
    if isinstance(inp, list) and inp and not PUBLIC:
        return [{"role": m.get("role", "user"), "content": str(m.get("content", ""))[:MAX_INPUT]} for m in inp]
    raise HTTPException(400, "input is required")


@router.post("/guide/sample")
async def guide_sample(request: Request, x_chat_code: str = Header(default="")):
    _gate(x_chat_code)
    body = await request.json()
    _, constitution = _page()
    msgs = _messages(body.get("input"), constitution)
    if body.get("tools"):
        return _tool_round(request, body, msgs)
    tier = "quick" if body.get("tier") == "quick" else "default"
    row = _admit(request.cookies.get("oo_v", ""), _network(request), tier, _is_owner(request))
    model = QUICK_MODEL if tier == "quick" else config.CLAUDE_MODEL
    kw = dict(model=model, max_tokens=8000, messages=msgs)
    if body.get("json"):
        kw["system"] = "Answer with a single JSON object only: no prose, no code fences."
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)

    if not body.get("stream"):
        import asyncio
        try:
            r = await asyncio.to_thread(lambda: client.messages.create(**kw))
        except anthropic.RateLimitError:
            raise HTTPException(429, detail={"reason": "busy"})
        _record(row, getattr(r, "usage", None))
        text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        return {"text": text}

    def events():
        try:
            with client.messages.stream(**kw) as s:
                for delta in s.text_stream:
                    yield f"event: text\ndata: {json.dumps(delta)}\n\n"
                _record(row, getattr(s.get_final_message(), "usage", None))
            yield "event: done\ndata: \"\"\n\n"
        except Exception as exc:  # noqa: BLE001
            log.exception("guide stream failed")
            yield f"event: error\ndata: {json.dumps(str(exc))}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _tool_round(request: Request, body: dict, msgs: list[dict]):
    """One round of a plain-mode reply. Claude gets the two research tools; when it calls them, the page runs them
    in the visitor's browser and sends the next round. The last round may not call a tool, so every reply ends."""
    if msgs[0]["role"] != "user" or not _plain_turns(body.get("input")):
        raise HTTPException(400, "tool rounds are only for the guide page's conversation")
    n = int(body.get("round") or 0)
    if not 0 <= n < MAX_ROUNDS:
        raise HTTPException(400, "too many rounds")
    row = _admit(request.cookies.get("oo_v", ""), _network(request), "default" if n == 0 else "round", _is_owner(request))
    kw = dict(model=config.CLAUDE_MODEL, max_tokens=16000, messages=msgs, tools=KG_TOOLS,
              output_config={"effort": config.CLAUDE_EFFORT or "medium"},
              cache_control={"type": "ephemeral"},            # the growing conversation is read again every round
              betas=["server-side-fallback-2026-07-01"], fallbacks="default")   # a declined request is retried on the recommended model
    if body.get("last") or n == MAX_ROUNDS - 1:
        kw["tool_choice"] = {"type": "none"}
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)

    def events():
        try:
            with client.beta.messages.stream(**kw) as s:
                for delta in s.text_stream:
                    yield f"event: text\ndata: {json.dumps(delta)}\n\n"
                final = s.get_final_message()
            _record(row, getattr(final, "usage", None))
            content = [b.to_dict() for b in final.content]
            yield f"event: final\ndata: {json.dumps({'content': content, 'stop_reason': final.stop_reason})}\n\n"
        except anthropic.RateLimitError:
            yield f"event: error\ndata: {json.dumps('busy')}\n\n"
        except Exception as exc:  # noqa: BLE001
            log.exception("guide tool round failed")
            yield f"event: error\ndata: {json.dumps(str(exc))}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- saved conversations

def _chats_db():
    """Postgres when DATABASE_URL is set, else the local SQLite file. Same table and SQL either way."""
    if DB_URL:
        import psycopg
        c = psycopg.connect(DB_URL, autocommit=True)
        ph = "%s"
    else:
        c = sqlite3.connect(DB, timeout=10)
        ph = "?"
    c.execute("""CREATE TABLE IF NOT EXISTS guide_chats (
                   conv_id TEXT PRIMARY KEY, visitor TEXT, started DOUBLE PRECISION NOT NULL,
                   updated DOUBLE PRECISION NOT NULL, turns INTEGER NOT NULL, first_msg TEXT, transcript TEXT NOT NULL)""")
    return c, ph


def _run(sql: str, args: tuple = (), fetch: bool = False):
    c, ph = _chats_db()
    try:
        cur = c.execute(sql.replace("?", ph), args)
        rows = cur.fetchall() if fetch else None
        if not DB_URL:
            c.commit()
        return rows
    finally:
        c.close()


@router.post("/guide/log")
async def guide_log(request: Request):
    """The page sends the whole conversation after each reply; one row per conversation, replaced as it grows."""
    if not LOG_ON:
        raise HTTPException(404)
    body = await request.json()
    conv = str(body.get("conv", ""))
    tr = body.get("transcript")
    if not (8 <= len(conv) <= 40 and conv.isalnum()) or not isinstance(tr, list) or not tr or len(tr) > 400:
        raise HTTPException(400, "bad conversation")
    turns = [{"role": "me" if t.get("role") == "me" else "guide", "text": str(t.get("text", ""))[:20000]}
             for t in tr if isinstance(t, dict)]
    data = json.dumps(turns, ensure_ascii=False)
    if len(data) > 400_000:
        raise HTTPException(413, "too long")
    visitor = request.cookies.get("oo_v", "")
    now = time.time()
    import asyncio

    def save():
        old = _run("SELECT visitor FROM guide_chats WHERE conv_id=?", (conv,), fetch=True)
        if old and (old[0][0] or "") != visitor:
            raise HTTPException(409, "not yours")
        if not old and visitor:
            n = _run("SELECT count(*) FROM guide_chats WHERE visitor=? AND started>?", (visitor, now - 86400), fetch=True)[0][0]
            if n >= LOG_NEW_PER_DAY:
                raise HTTPException(429, detail={"reason": "day"})
        first = next((t["text"] for t in turns if t["role"] == "me"), "")[:300]
        _run("""INSERT INTO guide_chats (conv_id, visitor, started, updated, turns, first_msg, transcript)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT (conv_id) DO UPDATE SET updated=excluded.updated, turns=excluded.turns,
                  first_msg=excluded.first_msg, transcript=excluded.transcript""",
             (conv, visitor, now, now, sum(1 for t in turns if t["role"] == "me"), first, data))
        _run("DELETE FROM guide_chats WHERE updated < ?", (now - 86400 * max(1, LOG_DAYS),))

    await asyncio.to_thread(save)
    return {"ok": True}


def _owner(code: str) -> None:
    if not OWNER_CODE or not hmac.compare_digest(code or "", OWNER_CODE):
        raise HTTPException(404)


@router.get("/guide/chats", response_class=HTMLResponse)
def guide_chats(code: str = "", q: str = "", days: int = 30, view: str = "chats") -> HTMLResponse:
    """For the owner only (GUIDE_OWNER_CODE): every saved conversation, newest first, with a search box."""
    _owner(code)
    import html as H
    since = time.time() - 86400 * max(1, min(days, 3650))
    rows = _run("SELECT conv_id, started, updated, turns, transcript FROM guide_chats WHERE updated > ? ORDER BY updated DESC LIMIT 1000",
                (since,), fetch=True) if LOG_ON else []
    ql = q.strip().lower()
    items = []
    if view == "questions":   # every message people wrote, newest first: what they actually ask
        skip = {"go deeper on that.", "go deeper"}
        for conv, started, updated, turns, tr in rows:
            msgs = json.loads(tr)
            when = time.strftime("%b %d, %H:%M", time.gmtime(updated))
            for i, m in enumerate(msgs):
                t = m["text"].strip()
                if m["role"] != "me" or t.lower() in skip or (ql and ql not in t.lower()):
                    continue
                reply = next((x["text"] for x in msgs[i + 1:] if x["role"] == "guide"), "")
                items.append(f'<details><summary><span class="when">{when}</span>{H.escape(t[:400])}</summary><div class="m guide"><b>Guide</b>{H.escape(reply)}</div></details>')
    for conv, started, updated, turns, tr in (rows if view != "questions" else []):
        if ql and ql not in tr.lower():
            continue
        msgs = json.loads(tr)
        first = next((m["text"] for m in msgs if m["role"] == "me"), "")
        when = time.strftime("%a %b %d, %H:%M UTC", time.gmtime(updated))
        body = "".join(f'<div class="m {m["role"]}"><b>{"Them" if m["role"] == "me" else "Guide"}</b>{H.escape(m["text"])}</div>' for m in msgs)
        items.append(f'<details><summary><span class="when">{when} · {turns} message{"s" if turns != 1 else ""}</span>{H.escape(first[:160])}</summary>{body}</details>')
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>Only One chats</title><style>
:root{{--bg:#f6f4ef;--card:#fff;--ink:#1f2328;--muted:#6b6f76;--line:#dcd8cf;--me:#eef3fb}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16181b;--card:#1f2226;--ink:#e8e6e1;--muted:#9aa0a6;--line:#33373c;--me:#1d2a3a}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,sans-serif}}
.wrap{{max-width:820px;margin:0 auto;padding:20px 16px 60px}} h1{{font-size:24px;margin:0 0 4px}} .note{{color:var(--muted);font-size:14px;margin:0 0 14px}}
form{{display:flex;gap:8px;margin:0 0 16px}} input{{flex:1;font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}}
button{{font:inherit;padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink)}}
details{{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:0 0 8px;padding:10px 14px}} summary{{cursor:pointer}}
.when{{display:block;font-size:13px;color:var(--muted)}} .m{{white-space:pre-wrap;padding:8px 10px;border-radius:8px;margin:8px 0;overflow-wrap:anywhere}}
.m.me{{background:var(--me)}} .tabs a{{margin-right:14px;color:var(--muted);text-decoration:none}} .tabs a.on{{color:var(--ink);font-weight:600;border-bottom:2px solid var(--ink)}} .m b{{display:block;font-size:12px;color:var(--muted)}}
</style></head><body><div class="wrap"><h1>Saved conversations</h1>
<p class="note">{len(items)} {"question" if view == "questions" else "conversation"}{"s" if len(items) != 1 else ""} in the last {days} days{(' matching "' + H.escape(q) + '"') if q else ''}. Kept {LOG_DAYS} days. {'' if LOG_ON else 'Saving is off: set DATABASE_URL on the host.'}</p>
<p class="tabs"><a href="?code={H.escape(code)}&days={days}"{' class="on"' if view != "questions" else ''}>Conversations</a> <a href="?code={H.escape(code)}&days={days}&view=questions"{' class="on"' if view == "questions" else ''}>Questions</a></p>
<form method="get"><input type="hidden" name="code" value="{H.escape(code)}"><input type="hidden" name="view" value="{H.escape(view)}"><input name="q" value="{H.escape(q)}" placeholder="Search the chats"><input type="hidden" name="days" value="{days}"><button>Search</button></form>
{''.join(items) or '<p class="note">Nothing yet.</p>'}</div></body></html>"""
    return HTMLResponse(doc, headers={"Cache-Control": "no-store"})


@router.get("/guide/chats.json")
def guide_chats_json(code: str = "", days: int = 30) -> JSONResponse:
    """The same conversations as data, for reading them in bulk."""
    _owner(code)
    rows = _run("SELECT conv_id, started, updated, turns, transcript FROM guide_chats WHERE updated > ? ORDER BY updated DESC",
                (time.time() - 86400 * max(1, min(days, 3650)),), fetch=True) if LOG_ON else []
    return JSONResponse([{"id": r[0], "started": r[1], "updated": r[2], "turns": r[3], "messages": json.loads(r[4])} for r in rows],
                        headers={"Cache-Control": "no-store"})


ELEVEN_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVEN_VOICE = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
ELEVEN_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2")
TTS_CACHE = Path(os.getenv("GUIDE_TTS_CACHE") or Path(__file__).resolve().parent.parent / "tts_cache")


@router.post("/guide/tts")
async def guide_tts(request: Request, x_chat_code: str = Header(default="")):
    """One paragraph of a meditation read aloud by ElevenLabs (the key stays on this server). The page asks
    paragraph by paragraph and keeps the script's pauses itself. Counted against the same ceilings as the
    small calls; identical paragraphs are served from a cache."""
    _gate(x_chat_code)
    if not (ELEVEN_KEY and ELEVEN_VOICE):
        raise HTTPException(404, "no voice configured")
    body = await request.json()
    text = str(body.get("text", "")).strip()[:1500]
    if not text:
        raise HTTPException(400, "text is required")
    import hashlib, urllib.request, asyncio
    h = hashlib.sha256(f"{ELEVEN_VOICE}|{ELEVEN_MODEL}|{text}".encode()).hexdigest()[:32]
    f = TTS_CACHE / f"{h}.mp3"
    if not f.exists():
        _admit(request.cookies.get("oo_v", ""), _network(request), "quick", _is_owner(request))
        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}?output_format=mp3_44100_128",
            data=json.dumps({"text": text, "model_id": ELEVEN_MODEL, "previous_text": str(body.get("prev", ""))[-500:],
                             "voice_settings": {"stability": 0.6, "similarity_boost": 0.75, "style": 0.1, "speed": 0.9}}).encode(),
            headers={"xi-api-key": ELEVEN_KEY, "Content-Type": "application/json"})
        try:
            audio = await asyncio.to_thread(lambda: urllib.request.urlopen(req, timeout=90).read())
        except Exception as exc:  # noqa: BLE001
            log.warning("tts failed: %s", exc)
            raise HTTPException(502, "the voice is unavailable right now")
        TTS_CACHE.mkdir(parents=True, exist_ok=True)
        f.write_bytes(audio)
    return Response(f.read_bytes(), media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/guide/stats")
def guide_stats(x_owner_code: str = Header(default=""), days: int = 14) -> JSONResponse:
    """For the owner: calls, visitors and tokens per day (no text is ever stored). Needs GUIDE_OWNER_CODE."""
    if not OWNER_CODE or not hmac.compare_digest(x_owner_code, OWNER_CODE):
        raise HTTPException(404)
    with _lock, _db() as c:
        rows = c.execute("""SELECT date(ts,'unixepoch') d, tier, count(*), count(DISTINCT visitor), sum(input),
                                   sum(output), sum(cache_read), sum(cache_write)
                            FROM guide_usage WHERE ts > ? GROUP BY d, tier ORDER BY d DESC, tier""",
                         (time.time() - 86400 * max(1, min(days, 30)),)).fetchall()
    return JSONResponse({"public": PUBLIC,
                         "ceilings": {"per_hour": PER_HOUR, "per_day": PER_DAY, "network_factor": NET_FACTOR,
                                      "site_per_day": SITE_PER_DAY},
                         "days": [dict(zip(("day", "tier", "calls", "visitors", "input_tokens", "output_tokens",
                                            "cache_read_tokens", "cache_write_tokens"), r)) for r in rows]})
