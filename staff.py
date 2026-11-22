import discord
from discord import app_commands
from discord.ext import commands

from perms import (
    ADMIN,
    COMMAND_DEFAULTS,
    COMMAND_LABELS,
    FIXED_ADMIN_LABELS,
    MOD,
    TIER_NAMES,
    TIMEOUT_CAPS,
    TRIAL,
    require_tier,
)
from utils import COLORS, format_duration, make_embed, truncate

TIER_CHOICES = [
    app_commands.Choice(name="Trial Mod", value=TRIAL),
    app_commands.Choice(name="Mod", value=MOD),
    app_commands.Choice(name="Admin", value=ADMIN),
]
COMMAND_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in COMMAND_LABELS.items()]


class Staff(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    permissions = app_commands.Group(
        name="permissions",
        description="Set up staff permission groups (Trial Mod, Mod, Admin)",
        guild_only=True,
    )

    @permissions.command(name="addrole", description="Put a role into a staff group")
    @require_tier(ADMIN)
    @app_commands.describe(tier="Which group this role belongs to", role="The role to add")
    @app_commands.choices(tier=TIER_CHOICES)
    async def addrole(self, interaction: discord.Interaction, tier: app_commands.Choice[int], role: discord.Role):
        if role.is_default():
            return await interaction.response.send_message("You can't use @everyone as a staff role.", ephemeral=True)
        if role.managed:
            return await interaction.response.send_message(
                "That role belongs to a bot or integration. Pick a normal role.", ephemeral=True
            )
        await self.bot.db.set_tier_role(interaction.guild.id, role.id, tier.value)
        await interaction.response.send_message(
            f"✅ {role.mention} is now in the **{tier.name}** group. (A role can only be in one group, "
            f"so if it was in another, it moved.)",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @permissions.command(name="removerole", description="Take a role out of its staff group")
    @require_tier(ADMIN)
    async def removerole(self, interaction: discord.Interaction, role: discord.Role):
        removed = await self.bot.db.remove_tier_role(interaction.guild.id, role.id)
        msg = f"Removed {role.mention} from its staff group." if removed else f"{role.mention} wasn't in a staff group."
        await interaction.response.send_message(msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @permissions.command(name="command", description="Change which group is needed for a command")
    @require_tier(ADMIN)
    @app_commands.describe(command="The command to change", tier="The lowest group allowed to use it")
    @app_commands.choices(
        command=COMMAND_CHOICES,
        tier=TIER_CHOICES + [app_commands.Choice(name="Reset to default", value=0)],
    )
    async def set_command(
        self, interaction: discord.Interaction, command: app_commands.Choice[str], tier: app_commands.Choice[int]
    ):
        gid = interaction.guild.id
        if tier.value == 0:
            await self.bot.db.clear_command_tier(gid, command.value)
            default = TIER_NAMES[COMMAND_DEFAULTS[command.value]]
            return await interaction.response.send_message(
                f"`{command.name}` is back to its default: **{default}** and up.", ephemeral=True
            )
        await self.bot.db.set_command_tier(gid, command.value, tier.value)
        await interaction.response.send_message(
            f"`{command.name}` now needs **{tier.name}** or higher.", ephemeral=True
        )

    @permissions.command(name="list", description="Show the staff groups, their roles and what each can do")
    @require_tier(ADMIN)
    async def list_groups(self, interaction: discord.Interaction):
        guild = interaction.guild
        rows = await self.bot.db.get_tier_roles(guild.id)
        by_tier = {TRIAL: [], MOD: [], ADMIN: []}
        for row in rows:
            if guild.get_role(row["role_id"]):
                by_tier[row["tier"]].append(f"<@&{row['role_id']}>")
        needs = {key: await self.bot.perms.required_tier(guild.id, key) for key in COMMAND_DEFAULTS}

        embed = make_embed("Staff permission groups", COLORS["blue"])
        for tier in (ADMIN, MOD, TRIAL):
            commands_here = [COMMAND_LABELS[k] for k, t in needs.items() if t == tier]
            roles = " ".join(by_tier[tier]) or "*no roles assigned yet*"
            lines = [
                f"**Roles:** {roles}",
                f"**Unlocks:** {', '.join(commands_here) if commands_here else 'nothing extra'}",
                f"**Longest timeout:** {format_duration(TIMEOUT_CAPS[tier])}",
            ]
            if tier == ADMIN:
                lines.append(f"**Always admin-only:** {', '.join(FIXED_ADMIN_LABELS)}")
            embed.add_field(name=TIER_NAMES[tier], value=truncate("\n".join(lines)), inline=False)
        embed.set_footer(
            text="Each group includes everything in the groups below it. The server owner and anyone with the "
            "Administrator permission always count as Admin."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @permissions.command(name="mytier", description="See which staff group you're in")
    async def mytier(self, interaction: discord.Interaction):
        tier = await self.bot.perms.member_tier(interaction.user)
        await interaction.response.send_message(f"You're in the **{TIER_NAMES[tier]}** group.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Staff(bot))
