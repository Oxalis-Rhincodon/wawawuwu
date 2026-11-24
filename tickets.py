import asyncio
import io
import logging
import time
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from perms import ADMIN, require, require_tier
from utils import COLORS, format_duration, make_embed, safe_channel_name, truncate, who

log = logging.getLogger("modbot.tickets")

MAX_OPEN_PER_USER = 2


TICKET_TYPES = {
    "support": ("Support", "🎫", "Get help from the staff team"),
    "report": ("Report", "🚨", "Report a member or an incident"),
    "appeal": ("Appeal", "⚖️", "Appeal a warning or timeout"),
}


# ===================================================================== views
class TicketModal(discord.ui.Modal):
    subject = discord.ui.TextInput(label="Subject", placeholder="Briefly, what do you need?", max_length=100)
    details = discord.ui.TextInput(
        label="Details",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
        placeholder="Give us as much detail as you can.",
    )

    def __init__(self, ticket_type: str):
        super().__init__(title=f"{TICKET_TYPES[ticket_type][0]} ticket")
        self.ticket_type = ticket_type

    async def on_submit(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Tickets")
        await cog.create_ticket(interaction, self.ticket_type, str(self.subject), str(self.details))


class CloseModal(discord.ui.Modal, title="Close this ticket"):
    reason = discord.ui.TextInput(
        label="Reason (optional)", required=False, max_length=300, placeholder="Resolved, duplicate, no reply..."
    )

    async def on_submit(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Tickets")
        await cog.do_close(interaction, str(self.reason) or "No reason provided")


class TicketPanelView(discord.ui.View):
    """The buttons on the public panel. Persistent: keeps working after restarts."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _open(self, interaction: discord.Interaction, ticket_type: str):
        cog = interaction.client.get_cog("Tickets")
        error = await cog.can_open(interaction.user, interaction.guild)
        if error:
            return await interaction.response.send_message(error, ephemeral=True)
        await interaction.response.send_modal(TicketModal(ticket_type))

    @discord.ui.button(label="Support", emoji="🎫", style=discord.ButtonStyle.primary, custom_id="ticket:open:support")
    async def open_support(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open(interaction, "support")

    @discord.ui.button(label="Report", emoji="🚨", style=discord.ButtonStyle.danger, custom_id="ticket:open:report")
    async def open_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open(interaction, "report")

    @discord.ui.button(label="Appeal", emoji="⚖️", style=discord.ButtonStyle.secondary, custom_id="ticket:open:appeal")
    async def open_appeal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._open(interaction, "appeal")


class TicketControlView(discord.ui.View):
    """Claim / Close buttons inside each ticket. Persistent too."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", emoji="🙋", style=discord.ButtonStyle.secondary, custom_id="ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.get_cog("Tickets").do_claim(interaction)

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("Tickets")
        ticket = await cog.db.get_open_ticket(interaction.channel.id)
        if ticket is None:
            return await interaction.response.send_message("This isn't an open ticket.", ephemeral=True)
        if not await cog.can_manage(interaction.user, ticket):
            return await interaction.response.send_message(
                "Only the person who opened this ticket or staff can close it.", ephemeral=True
            )
        await interaction.response.send_modal(CloseModal())


# ======================================================================= cog
class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db

    async def cog_load(self):
        self.bot.add_view(TicketPanelView())
        self.bot.add_view(TicketControlView())

    # ---------------------------------------------------------------- helpers
    async def staff_tier(self, guild_id: int) -> int:
        return await self.bot.perms.required_tier(guild_id, "ticket_staff")

    async def is_staff(self, member) -> bool:
        need = await self.staff_tier(member.guild.id)
        return await self.bot.perms.member_tier(member) >= need

    async def can_manage(self, member, ticket) -> bool:
        return member.id == ticket["user_id"] or await self.is_staff(member)

    async def can_open(self, member: discord.Member, guild: discord.Guild) -> Optional[str]:
        cfg = await self.db.get_ticket_config(guild.id)
        if cfg is None or guild.get_channel(cfg["category_id"]) is None:
            return "Tickets aren't set up on this server yet. Ask an admin to run `/ticket setup`."
        open_tickets = await self.db.list_open_tickets(guild.id, member.id)
        if len(open_tickets) >= MAX_OPEN_PER_USER:
            mentions = ", ".join(f"<#{t['channel_id']}>" for t in open_tickets)
            return f"You already have {len(open_tickets)} open tickets ({mentions}). Close one before opening another."
        return None

    # ------------------------------------------------------------- create
    async def create_ticket(self, interaction: discord.Interaction, ticket_type: str, subject: str, details: str):
        await interaction.response.defer(ephemeral=True)
        guild, member = interaction.guild, interaction.user

        error = await self.can_open(member, guild)
        if error:
            return await interaction.followup.send(error, ephemeral=True)

        cfg = await self.db.get_ticket_config(guild.id)
        category = guild.get_channel(cfg["category_id"])
        staff_roles = await self.bot.perms.roles_at_least(guild, await self.staff_tier(guild.id))
        number = await self.db.next_ticket_number(guild.id)
        label, emoji, _ = TICKET_TYPES[ticket_type]

        can_talk = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        )
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: can_talk,
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
                manage_channels=True,
                manage_messages=True,
            ),
        }
        for role in staff_roles:
            overwrites[role] = can_talk

        try:
            channel = await guild.create_text_channel(
                f"{ticket_type}-{number:04d}",
                category=category,
                overwrites=overwrites,
                topic=f"Ticket #{number:04d} · {label} · opened by {member} ({member.id})",
                reason=f"Ticket opened by {member} ({member.id})",
            )
        except discord.HTTPException as e:
            return await interaction.followup.send(
                f"I couldn't create the ticket channel: {e}. Ask an admin to check my permissions.", ephemeral=True
            )

        await self.db.create_ticket(guild.id, number, channel.id, member.id, ticket_type, subject)

        embed = make_embed(f"{emoji} {label} ticket #{number:04d}", COLORS["blue"])
        embed.add_field(name="Opened by", value=who(member), inline=False)
        embed.add_field(name="Subject", value=truncate(subject, 200), inline=False)
        if details:
            embed.add_field(name="Details", value=truncate(details), inline=False)
        embed.set_footer(text="Staff will be with you shortly. Use the buttons below to claim or close this ticket.")

        ping = f"<@&{cfg['ping_role_id']}>" if cfg["ping_role_id"] else ""
        await channel.send(
            content=f"{member.mention} {ping}".strip(),
            embed=embed,
            view=TicketControlView(),
            allowed_mentions=discord.AllowedMentions(users=[member], roles=True),
        )
        await interaction.followup.send(f"Your ticket is ready: {channel.mention}", ephemeral=True)

        log_embed = make_embed(f"Ticket #{number:04d} opened", COLORS["green"])
        log_embed.add_field(name="Type", value=label)
        log_embed.add_field(name="Opened by", value=who(member))
        log_embed.add_field(name="Channel", value=channel.mention)
        log_embed.add_field(name="Subject", value=truncate(subject, 200), inline=False)
        await self.bot.send_log(guild, "tickets", log_embed)

    # -------------------------------------------------------------- claim
    async def do_claim(self, interaction: discord.Interaction):
        ticket = await self.db.get_open_ticket(interaction.channel.id)
        if ticket is None:
            return await interaction.response.send_message("This isn't an open ticket.", ephemeral=True)
        if not await self.is_staff(interaction.user):
            return await interaction.response.send_message("Only staff can claim tickets.", ephemeral=True)

        current = ticket["claimed_by"]
        if current == interaction.user.id:
            return await interaction.response.send_message("You've already claimed this ticket.", ephemeral=True)
        if current and await self.bot.perms.member_tier(interaction.user) < ADMIN:
            return await interaction.response.send_message(
                f"This ticket is already claimed by <@{current}>. Only admins can take it over.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        await self.db.set_ticket_claimed(ticket["id"], interaction.user.id)
        verb = "took over" if current else "claimed"
        await interaction.response.send_message(
            embed=discord.Embed(description=f"🙋 {interaction.user.mention} {verb} this ticket.", color=COLORS["green"])
        )
        log_embed = make_embed(f"Ticket #{ticket['number']:04d} {verb}", COLORS["blue"])
        log_embed.add_field(name="Staff member", value=who(interaction.user))
        log_embed.add_field(name="Channel", value=interaction.channel.mention)
        await self.bot.send_log(interaction.guild, "tickets", log_embed)

    # -------------------------------------------------------------- close
    async def build_transcript(self, channel: discord.TextChannel, ticket) -> bytes:
        lines = [
            f"Transcript for ticket #{ticket['number']:04d} ({ticket['type']})",
            f"Subject: {ticket['subject']}",
            f"Opened by user ID: {ticket['user_id']}",
            "-" * 60,
        ]
        async for m in channel.history(limit=1000, oldest_first=True):
            text = m.content or ""
            if m.attachments:
                text += " [attachments: " + ", ".join(a.url for a in m.attachments) + "]"
            for e in m.embeds:
                if e.title or e.description:
                    text += f" [embed: {e.title or ''} {truncate(e.description or '', 200)}]"
            lines.append(f"[{m.created_at:%Y-%m-%d %H:%M:%S} UTC] {m.author} ({m.author.id}): {text}")
        return "\n".join(lines).encode("utf-8")

    async def do_close(self, interaction: discord.Interaction, reason: str):
        channel = interaction.channel
        ticket = await self.db.get_open_ticket(channel.id)
        if ticket is None:
            return await interaction.response.send_message("This isn't an open ticket.", ephemeral=True)
        if not await self.can_manage(interaction.user, ticket):
            return await interaction.response.send_message(
                "Only the person who opened this ticket or staff can close it.", ephemeral=True
            )

        await interaction.response.defer()
        guild = interaction.guild
        number = ticket["number"]
        filename = f"transcript-{ticket['type']}-{number:04d}.txt"
        transcript = await self.build_transcript(channel, ticket)
        await self.db.close_ticket(ticket["id"], interaction.user.id, reason)

        open_for = format_duration(timedelta(seconds=int(time.time()) - ticket["created_at"]))
        embed = make_embed(f"Ticket #{number:04d} closed", COLORS["red"])
        embed.add_field(name="Type", value=ticket["type"].title())
        embed.add_field(name="Opened by", value=f"<@{ticket['user_id']}> (`{ticket['user_id']}`)")
        embed.add_field(name="Claimed by", value=f"<@{ticket['claimed_by']}>" if ticket["claimed_by"] else "*nobody*")
        embed.add_field(name="Closed by", value=who(interaction.user))
        embed.add_field(name="Open for", value=open_for)
        embed.add_field(name="Subject", value=truncate(ticket["subject"], 200), inline=False)
        embed.add_field(name="Close reason", value=truncate(reason, 300), inline=False)
        await self.bot.send_log(
            guild, "tickets", embed, file=discord.File(io.BytesIO(transcript), filename=filename)
        )

       
        opener = guild.get_member(ticket["user_id"])
        if opener:
            dm = make_embed(f"Your ticket in {guild.name} was closed", COLORS["red"])
            dm.add_field(name="Reason", value=truncate(reason, 300), inline=False)
            try:
                await opener.send(embed=dm, file=discord.File(io.BytesIO(transcript), filename=filename))
            except (discord.Forbidden, discord.HTTPException):
                pass

        await interaction.followup.send("🔒 Ticket closed. This channel will be deleted in 5 seconds.")
        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"Ticket #{number:04d} closed by {interaction.user} ({interaction.user.id})")
        except discord.HTTPException:
            log.exception("Couldn't delete ticket channel %s", channel.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        # If someone deletes a ticket channel by hand, free up the opener's ticket slot.
        await self.db.close_ticket_for_deleted_channel(channel.id)

    # ----------------------------------------------------------- slash group
    ticket = app_commands.Group(name="ticket", description="Support tickets", guild_only=True)

    async def _current_ticket(self, interaction: discord.Interaction):
        ticket = await self.db.get_open_ticket(interaction.channel.id)
        if ticket is None:
            await interaction.response.send_message("Run this inside an open ticket channel.", ephemeral=True)
        return ticket

    @ticket.command(name="setup", description="Choose the category tickets are created in")
    @require_tier(ADMIN)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @app_commands.describe(
        category="Category (folder of channels) where new tickets will appear",
        ping_role="Role to ping when a ticket opens (optional)",
    )
    async def ticket_setup(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        ping_role: Optional[discord.Role] = None,
    ):
        await self.db.set_ticket_config(interaction.guild.id, category.id, ping_role.id if ping_role else None)
        staff_roles = await self.bot.perms.roles_at_least(
            interaction.guild, await self.staff_tier(interaction.guild.id)
        )
        msg = f"✅ Tickets will be created in **{category.name}**."
        if ping_role:
            msg += f" {ping_role.mention} will be pinged when one opens."
        if staff_roles:
            msg += "\nStaff who can see tickets: " + " ".join(r.mention for r in staff_roles)
        else:
            msg += (
                "\n⚠️ No staff roles are assigned yet, so only people with the Administrator permission can see "
                "tickets. Use `/permissions addrole` to add some."
            )
        msg += "\nNext: run `/ticket panel` in the channel where members should open tickets."
        await interaction.response.send_message(msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @ticket.command(name="panel", description="Post the 'open a ticket' buttons in a channel")
    @require_tier(ADMIN)
    @app_commands.describe(
        channel="Where to post it (defaults to this channel)",
        title="Panel title",
        description="Text shown above the buttons",
    )
    async def ticket_panel(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
        title: app_commands.Range[str, 1, 100] = "Need help?",
        description: app_commands.Range[str, 1, 1000] = "Press a button below to open a private ticket with our staff team.",
    ):
        cfg = await self.db.get_ticket_config(interaction.guild.id)
        if cfg is None:
            return await interaction.response.send_message("Run `/ticket setup` first.", ephemeral=True)
        target = channel or interaction.channel
        embed = make_embed(title, COLORS["blue"], description)
        embed.timestamp = None
        for _, (label, emoji, blurb) in TICKET_TYPES.items():
            embed.add_field(name=f"{emoji} {label}", value=blurb, inline=False)
        try:
            await target.send(embed=embed, view=TicketPanelView())
        except discord.Forbidden:
            return await interaction.response.send_message(
                f"I can't send messages in {target.mention}. Give me View Channel, Send Messages and Embed Links there.",
                ephemeral=True,
            )
        await interaction.response.send_message(f"✅ Panel posted in {target.mention}.", ephemeral=True)

    @ticket.command(name="claim", description="Claim the ticket you're in")
    async def ticket_claim(self, interaction: discord.Interaction):
        await self.do_claim(interaction)

    @ticket.command(name="close", description="Close the ticket you're in")
    @app_commands.describe(reason="Why it's being closed")
    async def ticket_close(self, interaction: discord.Interaction, reason: app_commands.Range[str, 1, 300] = "No reason provided"):
        await self.do_close(interaction, reason)

    @ticket.command(name="add", description="Add someone to this ticket")
    @require("ticket_staff")
    async def ticket_add(self, interaction: discord.Interaction, member: discord.Member):
        ticket = await self._current_ticket(interaction)
        if ticket is None:
            return
        await interaction.channel.set_permissions(
            member,
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            reason=f"Added to ticket by {interaction.user}",
        )
        await interaction.response.send_message(f"➕ Added {member.mention} to this ticket.")

    @ticket.command(name="remove", description="Remove someone from this ticket")
    @require("ticket_staff")
    async def ticket_remove(self, interaction: discord.Interaction, member: discord.Member):
        ticket = await self._current_ticket(interaction)
        if ticket is None:
            return
        if member.id == ticket["user_id"]:
            return await interaction.response.send_message(
                "You can't remove the person who opened the ticket. Close it instead.", ephemeral=True
            )
        await interaction.channel.set_permissions(member, overwrite=None, reason=f"Removed by {interaction.user}")
        await interaction.response.send_message(f"➖ Removed {member.mention} from this ticket.")

    @ticket.command(name="rename", description="Rename this ticket channel")
    @require("ticket_staff")
    @app_commands.describe(name="New channel name")
    async def ticket_rename(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 80]):
        ticket = await self._current_ticket(interaction)
        if ticket is None:
            return
        new_name = safe_channel_name(name)
        await interaction.response.defer()
        try:
            await interaction.channel.edit(name=new_name, reason=f"Renamed by {interaction.user}")
        except discord.HTTPException as e:
            return await interaction.followup.send(f"Discord refused the rename: {e}", ephemeral=True)
        await interaction.followup.send(f"✏️ Renamed to **{new_name}**.")

    @ticket.command(name="list", description="List open tickets")
    @require("ticket_staff")
    @app_commands.describe(user="Only show tickets opened by this member")
    async def ticket_list(self, interaction: discord.Interaction, user: Optional[discord.User] = None):
        rows = await self.db.list_open_tickets(interaction.guild.id, user.id if user else None)
        if not rows:
            return await interaction.response.send_message("No open tickets.", ephemeral=True)
        lines = []
        for t in rows[:20]:
            claimed = f"claimed by <@{t['claimed_by']}>" if t["claimed_by"] else "unclaimed"
            lines.append(
                f"**#{t['number']:04d}** · <#{t['channel_id']}> · {t['type']} · <@{t['user_id']}> · "
                f"<t:{t['created_at']}:R> · {claimed}"
            )
        embed = make_embed(f"Open tickets ({len(rows)})", COLORS["blue"], "\n".join(lines))
        if len(rows) > 20:
            embed.set_footer(text=f"Showing 20 of {len(rows)}")
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(Tickets(bot))
