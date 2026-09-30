"""Environment configuration. Loads .env then falls back to the process env."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _req(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return v


def _opt(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


# Neo4j
NEO4J_URI = _opt("NEO4J_URI", "neo4j+s://2a58f34b.databases.neo4j.io")
NEO4J_USERNAME = _opt("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = _opt("NEO4J_PASSWORD")
NEO4J_DATABASE = _opt("NEO4J_DATABASE", "neo4j")

# Anthropic
ANTHROPIC_API_KEY = _opt("ANTHROPIC_API_KEY")
CLAUDE_MODEL = _opt("CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = _opt("CLAUDE_EFFORT", "medium")

# WhatsApp
WA_PHONE_NUMBER_ID = _opt("WHATSAPP_PHONE_NUMBER_ID")
WA_ACCESS_TOKEN = _opt("WHATSAPP_ACCESS_TOKEN")
WA_VERIFY_TOKEN = _opt("WHATSAPP_VERIFY_TOKEN")
WA_APP_SECRET = _opt("WHATSAPP_APP_SECRET")
WA_API_VERSION = _opt("WHATSAPP_API_VERSION", "v21.0")

ALLOWED_NUMBERS = [
    n.strip() for n in _opt("ALLOWED_NUMBERS").split(",") if n.strip()
]

# Twilio (alternative channel to Meta - no Meta app required)
TWILIO_ACCOUNT_SID = _opt("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = _opt("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_FROM = _opt("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
# The exact public URL Twilio posts to. Signature validation hashes the URL, so
# a tunnel that rewrites the scheme breaks it unless this is pinned.
TWILIO_WEBHOOK_URL = _opt("TWILIO_WEBHOOK_URL")

# Web chat (shared passphrase; the tunnel URL is public)
CHAT_ACCESS_CODE = _opt("CHAT_ACCESS_CODE")

# Embeddings
EMBEDDING_BACKEND = _opt("EMBEDDING_BACKEND", "local").lower()
OPENAI_API_KEY = _opt("OPENAI_API_KEY")

# WhatsApp hard limit on a single text body.
WA_MAX_CHARS = 4096
