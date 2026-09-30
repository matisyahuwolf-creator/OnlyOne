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
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

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


def _admit(visitor: str, network: str, tier: str) -> int:
    """Checks the ceilings and reserves this call. Returns the row id; raises 429 with a reason."""
    now = time.time()
    k = {"default": 1, "round": MAX_ROUNDS}.get(tier, 3)   # the small calls get three times the room; tool rounds, one reply's worth
    with _lock, _db() as c:
        c.execute("DELETE FROM guide_usage WHERE ts < ?", (now - 86400 * 30,))

        def count(where: str, args: tuple, since: float) -> int:
            return c.execute(f"SELECT count(*) FROM guide_usage WHERE tier=? AND ts>? {where}",
                             (tier, since) + args).fetchone()[0]

        if PUBLIC:
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

@router.get("/guide", response_class=HTMLResponse)
def guide_page(request: Request) -> HTMLResponse:
    html, _ = _page()
    head = '<!doctype html><html lang="en"><head><meta charset="utf-8">' \
           '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
    doc = head + SHIM.replace("__ROUNDS__", str(MAX_ROUNDS)).replace("__PUBLIC__", "true" if PUBLIC else "false").replace("__TTS__", "true" if (os.getenv("ELEVENLABS_API_KEY", "").strip() and os.getenv("ELEVENLABS_VOICE_ID", "").strip()) else "false") + "</head><body>" + html + "</body></html>"
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
    row = _admit(request.cookies.get("oo_v", ""), _network(request), tier)
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
    row = _admit(request.cookies.get("oo_v", ""), _network(request), "default" if n == 0 else "round")
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
        _admit(request.cookies.get("oo_v", ""), _network(request), "quick")
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
