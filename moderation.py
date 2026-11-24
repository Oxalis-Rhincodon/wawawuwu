import logging
import time
from collections import Counter
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from perms import ADMIN, TIER_NAMES, TIMEOUT_CAPS, require
from utils import COLORS, format_duration, make_embed, parse_duration, truncate, who

log = logging.getLogger("modbot.moderation")

Reason = app_commands.Range[str, 1, 400]
NO_REASON = "No reason provided"

ACTION_COLORS = {
    "warn": COLORS["yellow"],
    "timeout": COLORS["orange"],
    "kick": COLORS["orange"],
    "ban": COLORS["red"],
    "tempban": COLORS["red"],
}


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.tempban_loop.start()

    async def cog_unload(self):
        self.tempban_loop.cancel()

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def audit_reason(interaction: discord.Interaction, reason: str) -> str:
        return truncate(f"{interaction.user} ({interaction.user.id}): {reason}", 512)

    async def target_error(
        self, interaction: discord.Interaction, target: discord.Member, bot_action: bool = True
    ) -> Optional[str]:
        """Returns a message if the action should be blocked, otherwise None."""
        guild = interaction.guild
        if target.id == interaction.user.id:
            return "You can't do that to yourself."
        if target.id == guild.me.id:
            return "I can't do that to myself."
        if target.id == guild.owner_id:
            return "You can't moderate the server owner."
        if interaction.user.id != guild.owner_id:
            mine = await self.bot.perms.member_tier(interaction.user)
            theirs = await self.bot.perms.member_tier(target)
            if theirs >= 1 and theirs >= mine:
                return "You can't take action against staff at or above your own tier."
        if bot_action and target.top_role >= guild.me.top_role:
            return "That member's highest role is equal to or above mine, so I can't act on them."
        return None

    @staticmethod
    async def dm(user, guild: discord.Guild, verb: str, color: int, reason: str, extra: Optional[str] = None) -> bool:
        embed = make_embed(f"You were {verb} in {guild.name}", color)
        embed.add_field(name="Reason", value=truncate(reason), inline=False)
        if extra:
            embed.add_field(name="Details", value=extra, inline=False)
        try:
            await user.send(embed=embed)
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def log_mod(self, interaction, title, target, reason, color, fields=None):
        embed = make_embed(title, color)
        embed.add_field(name="User", value=who(target))
        embed.add_field(name="Moderator", value=who(interaction.user))
        for name, value in fields or []:
            embed.add_field(name=name, value=value)
        embed.add_field(name="Reason", value=truncate(reason), inline=False)
        if interaction.channel:
            embed.add_field(name="Used in", value=f"<#{interaction.channel.id}>")
        await self.bot.send_log(interaction.guild, "moderation", embed)

    async def record(
        self,
        interaction,
        target,
        action: str,
        reason: str,
        duration: Optional[timedelta] = None,
        expires_at: Optional[int] = None,
        dm_sent: Optional[bool] = None,
    ) -> int:
        case = await self.bot.db.add_infraction(
            interaction.guild.id, target.id, interaction.user.id, action, reason, expires_at
        )
        fields = []
        if duration:
            fields.append(("Duration", format_duration(duration)))
        if expires_at:
            fields.append(("Expires", f"<t:{expires_at}:R>"))
        if dm_sent is not None:
            fields.append(("DM sent", "Yes" if dm_sent else "No (DMs closed)"))
        await self.log_mod(
            interaction, f"Case #{case} · {action.title()}", target, reason, ACTION_COLORS[action], fields
        )
        return case

    @staticmethod
    def done(text: str, color: int) -> discord.Embed:
        return discord.Embed(description=text, color=color)

    # ------------------------------------------------------------------- warn
    @app_commands.command(name="warn", description="Warn a member and add it to their record")
    @app_commands.guild_only()
    @require("warn")
    @app_commands.describe(member="Who to warn", reason="Why they're being warned")
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: Reason):
        if err := await self.target_error(interaction, member, bot_action=False):
            return await interaction.response.send_message(err, ephemeral=True)
        await interaction.response.defer()
        dm_sent = await self.dm(member, interaction.guild, "warned", COLORS["yellow"], reason)
        case = await self.record(interaction, member, "warn", reason, dm_sent=dm_sent)
        total = await self.bot.db.count_infractions(interaction.guild.id, member.id, "warn")
        note = "" if dm_sent else " (couldn't DM them)"
        await interaction.followup.send(
            embed=self.done(
                f"⚠️ Warned {member.mention}{note} · Case #{case} · {total} warning(s) on record\n**Reason:** {reason}",
                COLORS["yellow"],
            )
        )

    # ---------------------------------------------------------------- timeout
    @app_commands.command(name="timeout", description="Time a member out (mute them for a set time)")
    @app_commands.guild_only()
    @require("timeout")
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    @app_commands.describe(member="Who to time out", duration="e.g. 10m, 2h, 1d, 1d12h", reason="Why")
    async def timeout(
        self, interaction: discord.Interaction, member: discord.Member, duration: str, reason: Reason = NO_REASON
    ):
        delta = parse_duration(duration)
        if delta is None:
            return await interaction.response.send_message(
                "I couldn't read that duration. Try something like `30m`, `2h`, `1d` or `1d12h`.", ephemeral=True
            )
        tier = await self.bot.perms.member_tier(interaction.user)
        cap = TIMEOUT_CAPS[min(max(tier, 1), ADMIN)]
        if delta > cap:
            return await interaction.response.send_message(
                f"Your tier ({TIER_NAMES[tier]}) can time people out for up to **{format_duration(cap)}**.",
                ephemeral=True,
            )
        if err := await self.target_error(interaction, member):
            return await interaction.response.send_message(err, ephemeral=True)

        await interaction.response.defer()
        try:
            await member.timeout(delta, reason=self.audit_reason(interaction, reason))
        except discord.HTTPException as e:
            return await interaction.followup.send(f"Discord refused that timeout: {e}", ephemeral=True)

        expires_at = int(time.time() + delta.total_seconds())
        dm_sent = await self.dm(
            member, interaction.guild, "timed out", COLORS["orange"], reason, f"Duration: {format_duration(delta)}"
        )
        case = await self.record(
            interaction, member, "timeout", reason, duration=delta, expires_at=expires_at, dm_sent=dm_sent
        )
        await interaction.followup.send(
            embed=self.done(
                f"🔇 Timed out {member.mention} for **{format_duration(delta)}** · Case #{case}\n**Reason:** {reason}",
                COLORS["orange"],
            )
        )

    @app_commands.command(name="untimeout", description="Remove a member's timeout")
    @app_commands.guild_only()
    @require("untimeout")
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member, reason: Reason = NO_REASON):
        if not member.is_timed_out():
            return await interaction.response.send_message("That member isn't timed out.", ephemeral=True)
        await interaction.response.defer()
        try:
            await member.timeout(None, reason=self.audit_reason(interaction, reason))
        except discord.HTTPException as e:
            return await interaction.followup.send(f"Discord refused that: {e}", ephemeral=True)
        await self.log_mod(interaction, "Timeout removed", member, reason, COLORS["green"])
        await interaction.followup.send(
            embed=self.done(f"🔈 Removed the timeout on {member.mention}.", COLORS["green"])
        )

    # ------------------------------------------------------------------- kick
    @app_commands.command(name="kick", description="Kick a member from the server")
    @app_commands.guild_only()
    @require("kick")
    @app_commands.checks.bot_has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: Reason = NO_REASON):
        if err := await self.target_error(interaction, member):
            return await interaction.response.send_message(err, ephemeral=True)
        await interaction.response.defer()
        dm_sent = await self.dm(member, interaction.guild, "kicked", COLORS["orange"], reason)
        try:
            await member.kick(reason=self.audit_reason(interaction, reason))
        except discord.HTTPException as e:
            return await interaction.followup.send(f"Discord refused that kick: {e}", ephemeral=True)
        case = await self.record(interaction, member, "kick", reason, dm_sent=dm_sent)
        await interaction.followup.send(
            embed=self.done(f"👢 Kicked **{member}** · Case #{case}\n**Reason:** {reason}", COLORS["orange"])
        )

    # -------------------------------------------------------------------- ban
    @app_commands.command(name="ban", description="Permanently ban a user")
    @app_commands.guild_only()
    @require("ban")
    @app_commands.checks.bot_has_permissions(ban_members=True)
    @app_commands.describe(user="Who to ban", reason="Why", delete_days="Days of their messages to delete (0-7)")
    async def ban(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: Reason = NO_REASON,
        delete_days: app_commands.Range[int, 0, 7] = 0,
    ):
        member = interaction.guild.get_member(user.id)
        if member and (err := await self.target_error(interaction, member)):
            return await interaction.response.send_message(err, ephemeral=True)
        if user.id == interaction.user.id:
            return await interaction.response.send_message("You can't ban yourself.", ephemeral=True)

        await interaction.response.defer()
        dm_sent = await self.dm(user, interaction.guild, "banned", COLORS["red"], reason) if member else None
        try:
            await interaction.guild.ban(
                user, reason=self.audit_reason(interaction, reason), delete_message_seconds=delete_days * 86400
            )
        except discord.HTTPException as e:
            return await interaction.followup.send(f"Discord refused that ban: {e}", ephemeral=True)
        case = await self.record(interaction, user, "ban", reason, dm_sent=dm_sent)
        await interaction.followup.send(
            embed=self.done(f"🔨 Banned **{user}** · Case #{case}\n**Reason:** {reason}", COLORS["red"])
        )

    @app_commands.command(name="tempban", description="Ban a user for a set amount of time")
    @app_commands.guild_only()
    @require("tempban")
    @app_commands.checks.bot_has_permissions(ban_members=True)
    @app_commands.describe(
        user="Who to ban",
        duration="e.g. 12h, 3d, 1w",
        reason="Why",
        delete_days="Days of their messages to delete (0-7)",
    )
    async def tempban(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        duration: str,
        reason: Reason = NO_REASON,
        delete_days: app_commands.Range[int, 0, 7] = 0,
    ):
        delta = parse_duration(duration)
        if delta is None:
            return await interaction.response.send_message(
                "I couldn't read that duration. Try something like `12h`, `3d` or `1w`.", ephemeral=True
            )
        member = interaction.guild.get_member(user.id)
        if member and (err := await self.target_error(interaction, member)):
            return await interaction.response.send_message(err, ephemeral=True)
        if user.id == interaction.user.id:
            return await interaction.response.send_message("You can't ban yourself.", ephemeral=True)

        await interaction.response.defer()
        expires_at = int(time.time() + delta.total_seconds())
        dm_sent = (
            await self.dm(
                user, interaction.guild, "temporarily banned", COLORS["red"], reason,
                f"Your ban ends <t:{expires_at}:R>.",
            )
            if member
            else None
        )
        try:
            await interaction.guild.ban(
                user,
                reason=self.audit_reason(interaction, f"[tempban {format_duration(delta)}] {reason}"),
                delete_message_seconds=delete_days * 86400,
            )
        except discord.HTTPException as e:
            return await interaction.followup.send(f"Discord refused that ban: {e}", ephemeral=True)
        case = await self.record(
            interaction, user, "tempban", reason, duration=delta, expires_at=expires_at, dm_sent=dm_sent
        )
        await interaction.followup.send(
            embed=self.done(
                f"⏳ Temp-banned **{user}** for **{format_duration(delta)}** · Case #{case}\n**Reason:** {reason}",
                COLORS["red"],
            )
        )

    @app_commands.command(name="unban", description="Unban a user by their ID")
    @app_commands.guild_only()
    @require("unban")
    @app_commands.checks.bot_has_permissions(ban_members=True)
    @app_commands.describe(user_id="The user's ID", reason="Why")
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: Reason = NO_REASON):
        if not user_id.strip().isdigit():
            return await interaction.response.send_message("That doesn't look like a user ID.", ephemeral=True)
        uid = int(user_id.strip())
        await interaction.response.defer()
        try:
            await interaction.guild.unban(discord.Object(id=uid), reason=self.audit_reason(interaction, reason))
        except discord.NotFound:
            return await interaction.followup.send("That user isn't banned.", ephemeral=True)
        except discord.HTTPException as e:
            return await interaction.followup.send(f"Discord refused that: {e}", ephemeral=True)
        await self.bot.db.deactivate_tempbans(interaction.guild.id, uid)
        try:
            target = await self.bot.fetch_user(uid)
        except discord.HTTPException:
            target = discord.Object(id=uid)
            target.mention = f"<@{uid}>"
        await self.log_mod(interaction, "Member unbanned", target, reason, COLORS["green"])
        await interaction.followup.send(embed=self.done(f"✅ Unbanned <@{uid}>.", COLORS["green"]))

    # ------------------------------------------------------------------ purge
    @app_commands.command(name="purge", description="Bulk delete recent messages in this channel")
    @app_commands.guild_only()
    @require("purge")
    @app_commands.checks.bot_has_permissions(manage_messages=True, read_message_history=True)
    @app_commands.describe(
        amount="How many recent messages to scan (1-500)",
        user="Only delete messages from this user",
        reason="Why",
    )
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 500],
        user: Optional[discord.User] = None,
        reason: Reason = NO_REASON,
    ):
        channel = interaction.channel
        if not hasattr(channel, "purge"):
            return await interaction.response.send_message("I can't purge this kind of channel.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        def check(m: discord.Message) -> bool:
            if m.pinned:
                return False
            return user is None or m.author.id == user.id

        try:
            deleted = await channel.purge(limit=amount, check=check, reason=self.audit_reason(interaction, reason))
        except discord.HTTPException as e:
            return await interaction.followup.send(f"Discord refused that: {e}", ephemeral=True)

        embed = make_embed("Messages purged", COLORS["orange"])
        embed.add_field(name="Moderator", value=who(interaction.user))
        embed.add_field(name="Channel", value=channel.mention)
        embed.add_field(name="Deleted", value=f"{len(deleted)} (scanned {amount})")
        if user:
            embed.add_field(name="Filtered to", value=who(user))
        embed.add_field(name="Reason", value=truncate(reason), inline=False)
        await self.bot.send_log(interaction.guild, "moderation", embed)
        await interaction.followup.send(
            f"🧹 Deleted **{len(deleted)}** message(s). Pinned messages are skipped.", ephemeral=True
        )

    # ---------------------------------------------------------------- history
    @app_commands.command(name="history", description="View a user's past offenses")
    @app_commands.guild_only()
    @require("history")
    async def history(self, interaction: discord.Interaction, user: discord.User):
        rows = await self.bot.db.get_infractions(interaction.guild.id, user.id)
        if not rows:
            return await interaction.response.send_message(f"{user.mention} has no offenses on record.", ephemeral=True)

        counts = Counter(r["action"] for r in rows)
        summary = " · ".join(f"{a} ×{c}" for a, c in counts.most_common())
        embed = make_embed(f"Offense history · {user}", COLORS["blue"], f"**{len(rows)}** total — {summary}")
        embed.set_thumbnail(url=user.display_avatar.url)
        for r in rows[:10]:
            extra = ""
            if r["action"] == "tempban" and r["expires_at"]:
                extra = f"\nExpires: <t:{r['expires_at']}:R>" if r["active"] else "\n(ban has ended)"
            embed.add_field(
                name=f"Case #{r['id']} · {r['action'].title()}",
                value=f"<t:{r['created_at']}:f> by <@{r['moderator_id']}>\n{truncate(r['reason'], 300)}{extra}",
                inline=False,
            )
        if len(rows) > 10:
            embed.set_footer(text=f"Showing the 10 most recent of {len(rows)}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="removecase", description="Delete a case from a user's record")
    @app_commands.guild_only()
    @require("removecase")
    @app_commands.describe(case_id="The case number shown in /history")
    async def removecase(self, interaction: discord.Interaction, case_id: int):
        row = await self.bot.db.get_infraction(interaction.guild.id, case_id)
        if row is None:
            return await interaction.response.send_message("I couldn't find that case.", ephemeral=True)
        await self.bot.db.delete_infraction(interaction.guild.id, case_id)
        embed = make_embed(f"Case #{case_id} removed", COLORS["grey"])
        embed.add_field(name="Moderator", value=who(interaction.user))
        embed.add_field(name="Original action", value=f"{row['action'].title()} on <@{row['user_id']}>")
        embed.add_field(name="Original reason", value=truncate(row["reason"]), inline=False)
        await self.bot.send_log(interaction.guild, "moderation", embed)
        await interaction.response.send_message(f"Removed case #{case_id}.", ephemeral=True)

    # ------------------------------------------------------- tempban expiry
    @tasks.loop(seconds=30)
    async def tempban_loop(self):
        for row in await self.bot.db.get_expired_tempbans(int(time.time())):
            await self.bot.db.deactivate(row["id"])
            guild = self.bot.get_guild(row["guild_id"])
            if guild is None:
                continue
            try:
                await guild.unban(discord.Object(id=row["user_id"]), reason=f"Tempban expired (case #{row['id']})")
            except discord.NotFound:
                pass  
            except discord.HTTPException:
                log.exception("Failed to auto-unban %s in %s", row["user_id"], guild.id)
                continue
            embed = make_embed("Tempban expired", COLORS["green"])
            embed.add_field(name="User", value=f"<@{row['user_id']}> (`{row['user_id']}`)")
            embed.add_field(name="Original case", value=f"#{row['id']}")
            await self.bot.send_log(guild, "moderation", embed)

    @tempban_loop.before_loop
    async def before_tempban_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Moderation(bot))
