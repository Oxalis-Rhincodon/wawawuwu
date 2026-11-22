import re
from datetime import timedelta
from typing import Optional

import discord

COLORS = {
    "red": 0xED4245,
    "green": 0x57F287,
    "yellow": 0xFEE75C,
    "blue": 0x5865F2,
    "orange": 0xE67E22,
    "grey": 0x95A5A6,
}

# key -> (channel name, topic). Every key gets its own log channel.
LOG_TYPES = {
    "joins": ("join-leave-logs", "Members joining and leaving"),
    "members": ("member-logs", "Nickname, role, username, avatar and timeout changes, plus autoroles"),
    "messages": ("message-logs", "Message edits, deletions and purges"),
    "voice": ("voice-logs", "Voice joins, leaves, moves, mutes and streams"),
    "channels": ("channel-logs", "Channel and thread creation, deletion and edits"),
    "roles": ("role-logs", "Role creation, deletion and edits"),
    "server": ("server-logs", "Server settings, emojis and invites"),
    "moderation": ("mod-logs", "Warns, timeouts, kicks, bans and other mod actions"),
    "tickets": ("ticket-logs", "Ticket opens, claims, closes and transcripts"),
    "commands": ("command-logs", "Every slash command that gets used"),
}


def make_embed(title: str, color: int = COLORS["blue"], description: Optional[str] = None) -> discord.Embed:
    return discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=discord.utils.utcnow(),
    )


def truncate(text: Optional[str], limit: int = 1000) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


_DURATION_RE = re.compile(r"(\d+)([smhdw])")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> Optional[timedelta]:
    """Parses things like '30m', '2h', '1d12h', '1w'. Returns None if invalid."""
    text = text.strip().lower().replace(" ", "")
    matches = _DURATION_RE.findall(text)
    if not matches or "".join(f"{n}{u}" for n, u in matches) != text:
        return None
    seconds = sum(int(n) * _UNITS[u] for n, u in matches)
    return timedelta(seconds=seconds) if seconds > 0 else None


def format_duration(td: timedelta) -> str:
    seconds = int(td.total_seconds())
    parts = []
    for name, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            qty, seconds = divmod(seconds, size)
            parts.append(f"{qty}{name}")
    return " ".join(parts) or "0s"


def who(user) -> str:
    return f"{user.mention} (`{user.id}`)"


def safe_channel_name(text: str) -> str:
    name = re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-")
    return name[:80] or "ticket"
