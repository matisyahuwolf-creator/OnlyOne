# Only One: the guide as a website

A chat anyone can open with no account: Claude, answering as itself, with the Only One research library on
Chassidus to search. The library lives in the page; when Claude searches it, the search runs in the visitor's
browser and this server only relays the rounds. You pay per use from the API key below.

Packed from the research repo by `mashpia-bot/scripts/pack_guide_site.py`. Don't edit here; change the
research repo and pack again.

## Run it

    pip install -r requirements.txt
    ANTHROPIC_API_KEY=... GUIDE_PUBLIC=1 uvicorn app.guide_server:app --port 8000

Then open http://localhost:8000.

## Settings

Set these in the host's environment settings. The key never goes in a file.

- `ANTHROPIC_API_KEY`: the key the replies are paid from.
- `CLAUDE_MODEL`: the model that answers (default `claude-opus-5-5`).
- `GUIDE_QUICK_MODEL`: the fast model for small calls (default `claude-haiku-4-5`).
- `GUIDE_PUBLIC=1`: anyone with the link, no code. Leave it off to require `CHAT_ACCESS_CODE`.
- `GUIDE_REPLIES_PER_HOUR` (default 30) and `GUIDE_REPLIES_PER_DAY` (100): the limits for each visitor.
- `GUIDE_NETWORK_FACTOR` (4): how many visitors one network (a home, an office) counts as.
- `GUIDE_SITE_REPLIES_PER_DAY` (2000): the limit for the whole site.
- `GUIDE_OWNER_CODE`: opens `/guide/stats` (calls, visitors and tokens per day) with the header `X-Owner-Code`.

Also set a monthly spend limit on the key in the Anthropic Console. That is the hard stop.

The usage counts live in `guide_usage.db` on the host's disk. On hosts whose disk resets on each
deploy, the counts start again, which only loosens the limits for that day.
