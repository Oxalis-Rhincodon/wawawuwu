import asyncio
import logging
import re
import time
from collections import deque
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from perms import ADMIN, require_tier
from utils import COLORS, make_embed, truncate, who

log = logging.getLogger("modbot.antispam")

SCHEMA = """
CREATE TABLE IF NOT EXISTS antispam_config (
    guild_id        INTEGER PRIMARY KEY,
    enabled         INTEGER NOT NULL DEFAULT 0,
    max_messages    INTEGER NOT NULL DEFAULT 5,
    per_seconds     INTEGER NOT NULL DEFAULT 5,
    dup_count       INTEGER NOT NULL DEFAULT 3,
    dup_seconds     INTEGER NOT NULL DEFAULT 20,
    max_mentions    INTEGER NOT NULL DEFAULT 5,
    block_invites   INTEGER NOT NULL DEFAULT 0,
    timeout_minutes INTEGER NOT NULL DEFAULT 10
);
CREATE TABLE IF NOT EXISTS antispam_ignored (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);
"""

DEFAULTS = {
    "enabled": 0,
    "max_messages": 5,
    "per_seconds": 5,
    "dup_count": 3,
    "dup_seconds": 20,
    "max_mentions": 5,
    "block_invites": 0,
    "timeout_minutes": 10,
}

INVITE_RE = re.compile(r"(?:discord\.gg|discord(?:app)?\.com/invite)/[\w-]+", re.I)
STRIKE_RESET_SECONDS = 300   # strikes are forgotten after 5 quiet minutes
COOLDOWN_SECONDS = 3         # right after a punishment, extra spam is just deleted


class AntiSpam(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.configs: dict[int, dict] = {}
        self.history: dict[tuple[int, int], deque] = {}
        self.strikes: dict[tuple[int, int], tuple[int, float]] = {}
        self.cooldown_until: dict[tuple[int, int], float] = {}

    async def cog_load(self):
        await self.bot.db.conn.executescript(SCHEMA)
        await self.bot.db.conn.commit()

    # ----------------------------------------------------------------- config
    async def get_config(self, guild_id: int) -> dict:
        if guild_id in self.configs:
            return self.configs[guild_id]
        cur = await self.bot.db.conn.execute("SELECT * FROM antispam_config WHERE guild_id = ?", (guild_id,))
        row = await cur.fetchone()
        cfg = dict(DEFAULTS)
        if row:
            for key in DEFAULTS:
                cfg[key] = row[key]
        cur = await self.bot.db.conn.execute("SELECT channel_id FROM antispam_ignored WHERE guild_id = ?", (guild_id,))
        cfg["ignored"] = {r["channel_id"] for r in await cur.fetchall()}
        cfg["enabled"] = bool(cfg["enabled"])
        cfg["block_invites"] = bool(cfg["block_invites"])
        self.configs[guild_id] = cfg
        return cfg

    async def update_config(self, guild_id: int, **fields):
        await self.bot.db.conn.execute("INSERT OR IGNORE INTO antispam_config (guild_id) VALUES (?)", (guild_id,))
        for key, value in fields.items():
            if key not in DEFAULTS:
                continue  # only known columns ever reach the SQL
            await self.bot.db.conn.execute(
                f"UPDATE antispam_config SET {key} = ? WHERE guild_id = ?", (int(value), guild_id)
            )
        await self.bot.db.conn.commit()
        self.configs.pop(guild_id, None)

    # --------------------------------------------------------------- detection
    @staticmethod
    def _normalise(content: str) -> str:
        return " ".join(content.lower().split())

    def check_message(self, message: discord.Message, cfg: dict, now: float):
        """Returns (reason, messages_to_delete, strikes_to_add) or None."""
        key = (message.guild.id, message.author.id)
        history = self.history.setdefault(key, deque(maxlen=40))
        history.append((now, message))

        if len(message.mentions) + len(message.role_mentions) >= cfg["max_mentions"]:
            return "mass mentions", [message], 2

        if cfg["block_invites"] and INVITE_RE.search(message.content or ""):
            return "posting invite links", [message], 1

        recent = [m for t, m in history if now - t <= cfg["per_seconds"]]
        if len(recent) >= cfg["max_messages"]:
            return "sending messages too fast", recent, 1

        text = self._normalise(message.content or "")
        if text:
            same = [m for t, m in history if now - t <= cfg["dup_seconds"] and self._normalise(m.content or "") == text]
            if len(same) >= cfg["dup_count"]:
                return "repeating the same message", same, 1
        return None

    async def delete_messages(self, messages):
        by_channel = {}
        for m in messages:
            by_channel.setdefault(m.channel, []).append(m)
        for channel, items in by_channel.items():
            try:
                if len(items) == 1:
                    await items[0].delete()
                else:
                    await channel.delete_messages(items)
            except (discord.NotFound, discord.HTTPException):
                pass

    # ------------------------------------------------------------------ events
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or not isinstance(message.author, discord.Member):
            return
        cfg = await self.get_config(message.guild.id)
        if not cfg["enabled"] or message.channel.id in cfg["ignored"]:
            return
        member = message.author
        if member.guild_permissions.manage_messages or await self.bot.perms.member_tier(member) >= 1:
            return  # staff are exempt

        now = time.monotonic()
        key = (message.guild.id, member.id)

        # Right after a punishment, quietly delete whatever else they fire off.
        if now < self.cooldown_until.get(key, 0):
            await self.delete_messages([message])
            return

        result = self.check_message(message, cfg, now)
        if result is None:
            return
        reason, to_delete, add_strikes = result

        # Set these before any await so a second message can't trigger a second punishment.
        self.cooldown_until[key] = now + COOLDOWN_SECONDS
        self.history.pop(key, None)
        count, last = self.strikes.get(key, (0, 0.0))
        if now - last > STRIKE_RESET_SECONDS:
            count = 0
        count += add_strikes
        self.strikes[key] = (count, now)

        await self.delete_messages(to_delete)
        await self.punish(message, member, reason, count, len(to_delete), cfg)

    async def punish(self, message, member: discord.Member, reason: str, strikes: int, deleted: int, cfg: dict):
        guild = message.guild
        timed_out = False
        minutes = cfg["timeout_minutes"]
        case = None

        if strikes >= 2:
            try:
                await member.timeout(timedelta(minutes=minutes), reason=f"Auto-mod: {reason}")
                timed_out = True
            except discord.HTTPException:
                log.warning("Couldn't time out %s in %s (check role order and Moderate Members)", member.id, guild.id)

        if timed_out:
            expires_at = int(time.time() + minutes * 60)
            case = await self.bot.db.add_infraction(
                guild.id, member.id, self.bot.user.id, "timeout", f"Auto-mod: {reason}", expires_at
            )
            try:
                dm = make_embed(f"You were timed out in {guild.name}", COLORS["orange"])
                dm.add_field(name="Reason", value=f"Auto-mod: {reason}", inline=False)
                dm.add_field(name="Length", value=f"{minutes} minute(s)")
                await member.send(embed=dm)
            except (discord.Forbidden, discord.HTTPException):
                pass
            notice = f"🔇 {member.mention} was timed out for {minutes} minute(s) for {reason}."
        else:
            notice = f"⚠️ {member.mention}, please slow down. You were caught {reason}."

        try:
            await message.channel.send(notice, delete_after=10, allowed_mentions=discord.AllowedMentions(users=[member]))
        except (discord.Forbidden, discord.HTTPException):
            pass

        title = f"Auto-mod: timeout (case #{case})" if timed_out else "Auto-mod: spam removed"
        embed = make_embed(title, COLORS["orange"] if timed_out else COLORS["yellow"])
        embed.add_field(name="User", value=who(member))
        embed.add_field(name="Reason", value=reason)
        embed.add_field(name="Messages deleted", value=str(deleted))
        embed.add_field(name="Channel", value=message.channel.mention)
        embed.add_field(name="Strikes", value=str(strikes))
        if message.content:
            embed.add_field(name="Last message", value=truncate(message.content, 500), inline=False)
        await self.bot.send_log(guild, "moderation", embed)

    # ---------------------------------------------------------------- commands
    antispam = app_commands.Group(name="antispam", description="Automatic spam protection", guild_only=True)

    @antispam.command(name="toggle", description="Turn anti-spam on or off")
    @require_tier(ADMIN)
    @app_commands.describe(enabled="True to turn it on, False to turn it off")
    async def toggle(self, interaction: discord.Interaction, enabled: bool):
        await self.update_config(interaction.guild.id, enabled=enabled)
        await interaction.response.send_message(
            f"🛡️ Anti-spam is now **{'ON' if enabled else 'OFF'}**.", ephemeral=True
        )

    @antispam.command(name="config", description="Change anti-spam settings (only what you fill in changes)")
    @require_tier(ADMIN)
    @app_commands.describe(
        max_messages="Messages allowed in the time window below (default 5)",
        per_seconds="The time window for max_messages, in seconds (default 5)",
        duplicates="Identical messages allowed before it counts as spam (default 3)",
        duplicate_seconds="Time window for duplicates, in seconds (default 20)",
        max_mentions="Mentions in a single message that count as mass-mentioning (default 5)",
        block_invites="Delete Discord invite links from non-staff (default off)",
        timeout_minutes="How long a repeat offender is timed out, in minutes (default 10)",
    )
    async def config(
        self,
        interaction: discord.Interaction,
        max_messages: Optional[app_commands.Range[int, 2, 20]] = None,
        per_seconds: Optional[app_commands.Range[int, 2, 30]] = None,
        duplicates: Optional[app_commands.Range[int, 2, 10]] = None,
        duplicate_seconds: Optional[app_commands.Range[int, 5, 120]] = None,
        max_mentions: Optional[app_commands.Range[int, 3, 30]] = None,
        block_invites: Optional[bool] = None,
        timeout_minutes: Optional[app_commands.Range[int, 1, 10080]] = None,
    ):
        changes = {
            "max_messages": max_messages,
            "per_seconds": per_seconds,
            "dup_count": duplicates,
            "dup_seconds": duplicate_seconds,
            "max_mentions": max_mentions,
            "block_invites": block_invites,
            "timeout_minutes": timeout_minutes,
        }
        changes = {k: v for k, v in changes.items() if v is not None}
        if not changes:
            return await interaction.response.send_message(
                "Fill in at least one setting to change. Use `/antispam status` to see the current ones.", ephemeral=True
            )
        await self.update_config(interaction.guild.id, **changes)
        await interaction.response.send_message("✅ Settings updated. Use `/antispam status` to review them.", ephemeral=True)

    @antispam.command(name="ignore", description="Don't run anti-spam in a channel")
    @require_tier(ADMIN)
    async def ignore(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.bot.db.conn.execute(
            "INSERT OR IGNORE INTO antispam_ignored (guild_id, channel_id) VALUES (?, ?)",
            (interaction.guild.id, channel.id),
        )
        await self.bot.db.conn.commit()
        self.configs.pop(interaction.guild.id, None)
        await interaction.response.send_message(f"Anti-spam will skip {channel.mention}.", ephemeral=True)

    @antispam.command(name="unignore", description="Run anti-spam in a channel again")
    @require_tier(ADMIN)
    async def unignore(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await self.bot.db.conn.execute(
            "DELETE FROM antispam_ignored WHERE guild_id = ? AND channel_id = ?", (interaction.guild.id, channel.id)
        )
        await self.bot.db.conn.commit()
        self.configs.pop(interaction.guild.id, None)
        await interaction.response.send_message(f"Anti-spam is active in {channel.mention} again.", ephemeral=True)

    @antispam.command(name="status", description="Show the current anti-spam settings")
    @require_tier(ADMIN)
    async def status(self, interaction: discord.Interaction):
        cfg = await self.get_config(interaction.guild.id)
        embed = make_embed("Anti-spam settings", COLORS["blue"])
        embed.add_field(name="Status", value="🟢 ON" if cfg["enabled"] else "🔴 OFF")
        embed.add_field(name="Flooding", value=f"{cfg['max_messages']} messages in {cfg['per_seconds']}s")
        embed.add_field(name="Repeats", value=f"{cfg['dup_count']} identical messages in {cfg['dup_seconds']}s")
        embed.add_field(name="Mass mentions", value=f"{cfg['max_mentions']}+ mentions in one message")
        embed.add_field(name="Invite links", value="Deleted" if cfg["block_invites"] else "Allowed")
        embed.add_field(name="Repeat offenders", value=f"Timed out for {cfg['timeout_minutes']} min")
        ignored = " ".join(f"<#{c}>" for c in cfg["ignored"]) or "*none*"
        embed.add_field(name="Ignored channels", value=ignored, inline=False)
        embed.set_footer(
            text="1st strike: messages deleted + warning. 2nd strike: timeout. Mass mentions go straight to a timeout. "
            "Staff and anyone with Manage Messages are exempt."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(AntiSpam(bot))
