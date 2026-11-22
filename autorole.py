import logging

import discord
from discord import app_commands
from discord.ext import commands

from perms import ADMIN, require_tier
from utils import COLORS, make_embed, who

log = logging.getLogger("modbot.autorole")


class Autorole(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    autorole = app_commands.Group(
        name="autorole",
        description="Automatically give new members a role",
        guild_only=True,
    )

    @autorole.command(name="set", description="Choose the role new members get when they join")
    @require_tier(ADMIN)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    @app_commands.describe(role="The role to give every new member")
    async def autorole_set(self, interaction: discord.Interaction, role: discord.Role):
        guild = interaction.guild
        if role.is_default():
            return await interaction.response.send_message("You can't use @everyone as an autorole.", ephemeral=True)
        if role.managed:
            return await interaction.response.send_message(
                "That role is managed by an integration or bot, so I can't hand it out.", ephemeral=True
            )
        if role >= guild.me.top_role:
            return await interaction.response.send_message(
                "That role is above (or equal to) my highest role. Drag my role above it in "
                "Server Settings → Roles, then try again.",
                ephemeral=True,
            )
        if interaction.user.id != guild.owner_id and role >= interaction.user.top_role:
            return await interaction.response.send_message(
                "That role is above (or equal to) your highest role, so you can't make it an autorole.",
                ephemeral=True,
            )
        await self.bot.db.set_autorole(guild.id, role.id)
        await interaction.response.send_message(
            f"✅ New members will now get {role.mention} when they join.", ephemeral=True
        )

    @autorole.command(name="clear", description="Stop giving new members a role")
    @require_tier(ADMIN)
    async def autorole_clear(self, interaction: discord.Interaction):
        await self.bot.db.clear_autorole(interaction.guild.id)
        await interaction.response.send_message("Autorole turned off.", ephemeral=True)

    @autorole.command(name="view", description="Show the current autorole")
    @require_tier(ADMIN)
    async def autorole_view(self, interaction: discord.Interaction):
        role_id = await self.bot.db.get_autorole(interaction.guild.id)
        role = interaction.guild.get_role(role_id) if role_id else None
        if role:
            await interaction.response.send_message(f"New members get {role.mention}.", ephemeral=True)
        elif role_id:
            await interaction.response.send_message(
                "An autorole was set, but that role no longer exists. Use `/autorole set` to pick a new one.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message("No autorole is set.", ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        role_id = await self.bot.db.get_autorole(member.guild.id)
        if not role_id:
            return
        role = member.guild.get_role(role_id)
        if role is None:
            return
        try:
            await member.add_roles(role, reason="Autorole")
        except discord.HTTPException:
            log.exception("Couldn't give autorole in guild %s", member.guild.id)
            return
        embed = make_embed("Autorole given", COLORS["green"])
        embed.add_field(name="User", value=who(member))
        embed.add_field(name="Role", value=role.mention)
        await self.bot.send_log(member.guild, "members", embed)


async def setup(bot):
    await bot.add_cog(Autorole(bot))
