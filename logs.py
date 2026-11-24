import io
import logging
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from perms import ADMIN, MOD, require_tier
from utils import COLORS, LOG_TYPES, make_embed, truncate, who

log = logging.getLogger("modbot.logs")


async def audit_actor(guild: discord.Guild, action: discord.AuditLogAction, target_id: int):
    """Best-effort lookup of who did something. Needs the View Audit Log permission."""
    try:
        async for entry in guild.audit_logs(limit=6, action=action):
            target = getattr(entry, "target", None)
            if target is not None and getattr(target, "id", None) == target_id:
                if (discord.utils.utcnow() - entry.created_at).total_seconds() < 20:
                    return entry.user, entry.reason
    except (discord.Forbidden, discord.HTTPException):
        pass
    return None, None


class Logs(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ------------------------------------------------------------------ setup
    @app_commands.command(name="setup_logs", description="Create a private category with a separate channel for each log type")
    @app_commands.guild_only()
    @require_tier(ADMIN)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def setup_logs(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, embed_links=True, attach_files=True
            ),
        }
        for staff_role in await self.bot.perms.roles_at_least(guild, MOD):
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True, read_message_history=True, send_messages=False
            )
        category = discord.utils.get(guild.categories, name="Mod Logs")
        if category is None:
            category = await guild.create_category("Mod Logs", overwrites=overwrites, reason="Log setup")

        created = []
        for key, (name, topic) in LOG_TYPES.items():
            existing_id = await self.bot.db.get_log_channel(guild.id, key)
            if existing_id and guild.get_channel(existing_id):
                continue
            channel = await guild.create_text_channel(
                name, category=category, topic=topic, reason="Log setup"
            )
            await self.bot.db.set_log_channel(guild.id, key, channel.id)
            created.append(channel.mention)

        if created:
            msg = "Created: " + ", ".join(created) + "\n\nThe category is private: only admins and roles in the Mod and Admin groups can read it."
        else:
            msg = "All log channels already exist. Use `/setlog` to point a log type at a different channel."
        await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(name="setlog", description="Point a log type at an existing channel")
    @app_commands.guild_only()
    @require_tier(ADMIN)
    @app_commands.describe(log_type="Which log to route", channel="Channel to send it to")
    @app_commands.choices(log_type=[app_commands.Choice(name=k, value=k) for k in LOG_TYPES])
    async def setlog(
        self,
        interaction: discord.Interaction,
        log_type: app_commands.Choice[str],
        channel: discord.TextChannel,
    ):
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                f"I need View Channel, Send Messages and Embed Links in {channel.mention} first.",
                ephemeral=True,
            )
        await self.bot.db.set_log_channel(interaction.guild.id, log_type.value, channel.id)
        await interaction.response.send_message(
            f"**{log_type.value}** logs will now go to {channel.mention}.", ephemeral=True
        )

    # --------------------------------------------------------------- commands
    @commands.Cog.listener()
    async def on_app_command_completion(self, interaction: discord.Interaction, command):
        if not interaction.guild:
            return
        params = ", ".join(f"{k}={v}" for k, v in interaction.namespace)
        embed = make_embed("Command used", COLORS["grey"])
        embed.add_field(name="User", value=who(interaction.user))
        embed.add_field(name="Command", value=f"`/{command.qualified_name}`")
        if interaction.channel:
            embed.add_field(name="Channel", value=f"<#{interaction.channel.id}>")
        if params:
            embed.add_field(name="Options", value=truncate(params), inline=False)
        await self.bot.send_log(interaction.guild, "commands", embed)

    # --------------------------------------------------------------- messages
    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if not payload.guild_id:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        msg = payload.cached_message
        if msg and msg.author.bot:
            return

        embed = make_embed("Message deleted", COLORS["red"])
        embed.add_field(name="Channel", value=f"<#{payload.channel_id}>")
        embed.add_field(name="Message ID", value=f"`{payload.message_id}`")
        if msg:
            embed.description = truncate(msg.content or "*no text*", 4000)
            embed.add_field(name="Author", value=who(msg.author))
            if msg.attachments:
                embed.add_field(
                    name="Attachments",
                    value=truncate("\n".join(a.url for a in msg.attachments)),
                    inline=False,
                )
        else:
            embed.description = "*Message wasn't in my cache, so the content and author are unknown.*"
        await self.bot.send_log(guild, "messages", embed)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        if not payload.guild_id:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        lines = []
        for m in sorted(payload.cached_messages, key=lambda m: m.created_at):
            extra = f" [attachments: {', '.join(a.url for a in m.attachments)}]" if m.attachments else ""
            lines.append(f"[{m.created_at:%Y-%m-%d %H:%M:%S} UTC] {m.author} ({m.author.id}): {m.content}{extra}")

        embed = make_embed("Bulk message delete", COLORS["red"])
        embed.add_field(name="Channel", value=f"<#{payload.channel_id}>")
        embed.add_field(name="Messages deleted", value=str(len(payload.message_ids)))
        embed.add_field(name="Saved in transcript", value=f"{len(lines)} (only cached messages can be saved)")
        file = None
        if lines:
            file = discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename="deleted_messages.txt")
        await self.bot.send_log(guild, "messages", embed, file=file)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not after.guild or after.author.bot or before.content == after.content:
            return
        embed = make_embed("Message edited", COLORS["yellow"])
        embed.add_field(name="Before", value=truncate(before.content or "*empty*"), inline=False)
        embed.add_field(name="After", value=truncate(after.content or "*empty*"), inline=False)
        embed.add_field(name="Author", value=who(after.author))
        embed.add_field(name="Channel", value=after.channel.mention)
        embed.add_field(name="Jump", value=f"[Go to message]({after.jump_url})")
        await self.bot.send_log(after.guild, "messages", embed)

    # ------------------------------------------------------------ joins/leaves
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        embed = make_embed("Member joined", COLORS["green"])
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="User", value=who(member))
        embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"))
        embed.add_field(name="Member count", value=str(member.guild.member_count))
        if (discord.utils.utcnow() - member.created_at) < timedelta(days=7):
            embed.add_field(name="⚠ New account", value="Created less than 7 days ago", inline=False)
        await self.bot.send_log(member.guild, "joins", embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        embed = make_embed("Member left", COLORS["orange"])
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="User", value=who(member))
        if member.joined_at:
            embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R"))
        roles = [r.mention for r in member.roles if r != member.guild.default_role]
        if roles:
            embed.add_field(name="Roles", value=truncate(" ".join(roles)), inline=False)
        await self.bot.send_log(member.guild, "joins", embed)

    # ---------------------------------------------------------------- members
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        guild = after.guild

        if before.nick != after.nick:
            embed = make_embed("Nickname changed", COLORS["blue"])
            embed.add_field(name="User", value=who(after))
            embed.add_field(name="Before", value=before.nick or "*none*")
            embed.add_field(name="After", value=after.nick or "*none*")
            await self.bot.send_log(guild, "members", embed)

        added = [r for r in after.roles if r not in before.roles]
        removed = [r for r in before.roles if r not in after.roles]
        if added or removed:
            embed = make_embed("Member roles updated", COLORS["blue"])
            embed.add_field(name="User", value=who(after), inline=False)
            if added:
                embed.add_field(name="Added", value=" ".join(r.mention for r in added))
            if removed:
                embed.add_field(name="Removed", value=" ".join(r.mention for r in removed))
            await self.bot.send_log(guild, "members", embed)

        if before.timed_out_until != after.timed_out_until:
            if after.timed_out_until:
                embed = make_embed("Member timed out", COLORS["orange"])
                embed.add_field(name="User", value=who(after))
                embed.add_field(name="Until", value=discord.utils.format_dt(after.timed_out_until, "f"))
            else:
                embed = make_embed("Timeout removed", COLORS["green"])
                embed.add_field(name="User", value=who(after))
            await self.bot.send_log(guild, "members", embed)

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        changes = []
        if before.name != after.name:
            changes.append(f"**Username:** {before.name} → {after.name}")
        if before.global_name != after.global_name:
            changes.append(f"**Display name:** {before.global_name} → {after.global_name}")
        if before.avatar != after.avatar:
            changes.append("**Avatar changed**")
        if not changes:
            return
        for guild in after.mutual_guilds:
            embed = make_embed("User profile updated", COLORS["blue"], "\n".join(changes))
            embed.add_field(name="User", value=who(after))
            embed.set_thumbnail(url=after.display_avatar.url)
            await self.bot.send_log(guild, "members", embed)

    # ------------------------------------------------------------------ voice
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        events = []
        if before.channel != after.channel:
            if before.channel is None:
                events.append(("Joined voice", COLORS["green"], f"Joined {after.channel.mention}"))
            elif after.channel is None:
                events.append(("Left voice", COLORS["orange"], f"Left {before.channel.mention}"))
            else:
                events.append(("Moved voice channel", COLORS["blue"], f"{before.channel.mention} → {after.channel.mention}"))

        flags = (
            ("mute", "Server muted", "Server unmuted"),
            ("deaf", "Server deafened", "Server undeafened"),
            ("self_mute", "Self muted", "Self unmuted"),
            ("self_deaf", "Self deafened", "Self undeafened"),
            ("self_stream", "Started streaming", "Stopped streaming"),
            ("self_video", "Camera on", "Camera off"),
        )
        for attr, on_text, off_text in flags:
            b, a = getattr(before, attr), getattr(after, attr)
            if b != a:
                channel = after.channel or before.channel
                events.append((on_text if a else off_text, COLORS["grey"], channel.mention if channel else ""))

        for title, color, desc in events:
            embed = make_embed(title, color, desc)
            embed.add_field(name="User", value=who(member))
            await self.bot.send_log(member.guild, "voice", embed)

    # --------------------------------------------------------------- channels
    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        embed = make_embed("Channel created", COLORS["green"])
        embed.add_field(name="Channel", value=f"{channel.mention} (`{channel.id}`)")
        embed.add_field(name="Type", value=str(channel.type).replace("_", " ").title())
        await self.bot.send_log(channel.guild, "channels", embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        embed = make_embed("Channel deleted", COLORS["red"])
        embed.add_field(name="Name", value=f"#{channel.name} (`{channel.id}`)")
        embed.add_field(name="Type", value=str(channel.type).replace("_", " ").title())
        await self.bot.send_log(channel.guild, "channels", embed)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        changes = []
        if before.name != after.name:
            changes.append(f"**Name:** {before.name} → {after.name}")
        for attr, label in (
            ("topic", "Topic"),
            ("nsfw", "NSFW"),
            ("slowmode_delay", "Slowmode (s)"),
            ("bitrate", "Bitrate"),
            ("user_limit", "User limit"),
        ):
            b, a = getattr(before, attr, None), getattr(after, attr, None)
            if b != a:
                changes.append(f"**{label}:** {truncate(str(b), 200)} → {truncate(str(a), 200)}")
        if before.category_id != after.category_id:
            changes.append("**Category changed**")
        if before.overwrites != after.overwrites:
            changes.append("**Permission overwrites changed**")
        if not changes:
            return
        embed = make_embed("Channel updated", COLORS["yellow"], "\n".join(changes))
        embed.add_field(name="Channel", value=f"{after.mention} (`{after.id}`)")
        await self.bot.send_log(after.guild, "channels", embed)

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        embed = make_embed("Thread created", COLORS["green"])
        embed.add_field(name="Thread", value=f"{thread.mention} (`{thread.id}`)")
        if thread.parent:
            embed.add_field(name="Parent", value=thread.parent.mention)
        await self.bot.send_log(thread.guild, "channels", embed)

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread):
        embed = make_embed("Thread deleted", COLORS["red"])
        embed.add_field(name="Name", value=f"{thread.name} (`{thread.id}`)")
        await self.bot.send_log(thread.guild, "channels", embed)

    # ------------------------------------------------------------------ roles
    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        embed = make_embed("Role created", COLORS["green"])
        embed.add_field(name="Role", value=f"{role.mention} (`{role.id}`)")
        await self.bot.send_log(role.guild, "roles", embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        embed = make_embed("Role deleted", COLORS["red"])
        embed.add_field(name="Name", value=f"{role.name} (`{role.id}`)")
        await self.bot.send_log(role.guild, "roles", embed)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        changes = []
        if before.name != after.name:
            changes.append(f"**Name:** {before.name} → {after.name}")
        if before.color != after.color:
            changes.append(f"**Color:** {before.color} → {after.color}")
        if before.hoist != after.hoist:
            changes.append(f"**Hoisted:** {before.hoist} → {after.hoist}")
        if before.mentionable != after.mentionable:
            changes.append(f"**Mentionable:** {before.mentionable} → {after.mentionable}")
        if before.permissions != after.permissions:
            gained = [n.replace("_", " ") for n, v in after.permissions if v and not getattr(before.permissions, n)]
            lost = [n.replace("_", " ") for n, v in before.permissions if v and not getattr(after.permissions, n)]
            if gained:
                changes.append("**Permissions added:** " + ", ".join(gained))
            if lost:
                changes.append("**Permissions removed:** " + ", ".join(lost))
        if not changes:
            return
        embed = make_embed("Role updated", COLORS["yellow"], truncate("\n".join(changes), 4000))
        embed.add_field(name="Role", value=f"{after.mention} (`{after.id}`)")
        await self.bot.send_log(after.guild, "roles", embed)

    # ----------------------------------------------------------------- server
    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        changes = []
        if before.name != after.name:
            changes.append(f"**Name:** {before.name} → {after.name}")
        if before.icon != after.icon:
            changes.append("**Icon changed**")
        if before.owner_id != after.owner_id:
            changes.append(f"**Owner:** <@{before.owner_id}> → <@{after.owner_id}>")
        if before.verification_level != after.verification_level:
            changes.append(f"**Verification level:** {before.verification_level} → {after.verification_level}")
        if not changes:
            return
        await self.bot.send_log(after, "server", make_embed("Server updated", COLORS["yellow"], "\n".join(changes)))

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before, after):
        added = [e for e in after if e not in before]
        removed = [e for e in before if e not in after]
        if not added and not removed:
            return
        embed = make_embed("Emojis updated", COLORS["blue"])
        if added:
            embed.add_field(name="Added", value=" ".join(str(e) for e in added), inline=False)
        if removed:
            embed.add_field(name="Removed", value=", ".join(f":{e.name}:" for e in removed), inline=False)
        await self.bot.send_log(guild, "server", embed)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        if not invite.guild:
            return
        embed = make_embed("Invite created", COLORS["green"])
        embed.add_field(name="Code", value=f"`{invite.code}`")
        if invite.inviter:
            embed.add_field(name="Created by", value=who(invite.inviter))
        if invite.channel:
            embed.add_field(name="Channel", value=f"<#{invite.channel.id}>")
        embed.add_field(name="Max uses", value=str(invite.max_uses or "unlimited"))
        embed.add_field(name="Expires", value=f"after {invite.max_age}s" if invite.max_age else "never")
        await self.bot.send_log(invite.guild, "server", embed)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        if not invite.guild:
            return
        embed = make_embed("Invite deleted", COLORS["red"])
        embed.add_field(name="Code", value=f"`{invite.code}`")
        await self.bot.send_log(invite.guild, "server", embed)

    # ------------------------------------------- manual bans/unbans (not by me)
    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        actor, reason = await audit_actor(guild, discord.AuditLogAction.ban, user.id)
        if actor and actor.id == self.bot.user.id:
            return  
        embed = make_embed("Member banned (manual)", COLORS["red"])
        embed.add_field(name="User", value=who(user))
        embed.add_field(name="Moderator", value=who(actor) if actor else "*unknown*")
        embed.add_field(name="Reason", value=truncate(reason or "No reason provided"), inline=False)
        await self.bot.send_log(guild, "moderation", embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        actor, reason = await audit_actor(guild, discord.AuditLogAction.unban, user.id)
        if actor and actor.id == self.bot.user.id:
            return
        embed = make_embed("Member unbanned (manual)", COLORS["green"])
        embed.add_field(name="User", value=who(user))
        embed.add_field(name="Moderator", value=who(actor) if actor else "*unknown*")
        embed.add_field(name="Reason", value=truncate(reason or "No reason provided"), inline=False)
        await self.bot.send_log(guild, "moderation", embed)


async def setup(bot):
    await bot.add_cog(Logs(bot))
