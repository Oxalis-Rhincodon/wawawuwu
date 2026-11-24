"""Staff permission groups.

Three tiers, each including everything below it:
    1 = Trial Mod, 2 = Mod, 3 = Admin

Server roles are assigned to a tier with /permissions addrole. A member's tier is the highest
tier among their roles. The server owner and anyone with Discord's Administrator permission
always count as Admin, so you can never lock yourself out.
"""
from datetime import timedelta

import discord
from discord import app_commands

TRIAL, MOD, ADMIN = 1, 2, 3
TIER_NAMES = {0: "Everyone", TRIAL: "Trial Mod", MOD: "Mod", ADMIN: "Admin"}


COMMAND_DEFAULTS = {
    "warn": TRIAL,
    "history": TRIAL,
    "timeout": TRIAL,
    "untimeout": TRIAL,
    "ticket_staff": TRIAL,
    "kick": MOD,
    "ban": MOD,
    "tempban": MOD,
    "unban": MOD,
    "purge": MOD,
    "removecase": ADMIN,
}

COMMAND_LABELS = {
    "warn": "/warn",
    "history": "/history",
    "timeout": "/timeout",
    "untimeout": "/untimeout",
    "ticket_staff": "ticket staff access (see and handle tickets)",
    "kick": "/kick",
    "ban": "/ban",
    "tempban": "/tempban",
    "unban": "/unban",
    "purge": "/purge",
    "removecase": "/removecase",
}


FIXED_ADMIN_LABELS = [
    "/permissions",
    "/setup_logs",
    "/setlog",
    "/autorole",
    "/ticket setup",
    "/ticket panel",
]


TIMEOUT_CAPS = {
    TRIAL: timedelta(days=2),
    MOD: timedelta(days=7),
    ADMIN: timedelta(days=28),
}


class MissingTier(app_commands.CheckFailure):
    def __init__(self, needed: int):
        super().__init__(f"Needs tier {needed}")
        self.needed = needed


class PermissionManager:
    def __init__(self, db):
        self.db = db

    async def member_tier(self, member) -> int:
        if not isinstance(member, discord.Member):
            return 0
        if member.id == member.guild.owner_id or member.guild_permissions.administrator:
            return ADMIN
        rows = await self.db.get_tier_roles(member.guild.id)
        role_ids = {r.id for r in member.roles}
        tier = 0
        for row in rows:
            if row["role_id"] in role_ids:
                tier = max(tier, row["tier"])
        return tier

    async def required_tier(self, guild_id: int, key: str) -> int:
        override = await self.db.get_command_tier(guild_id, key)
        return override if override is not None else COMMAND_DEFAULTS[key]

    async def roles_at_least(self, guild: discord.Guild, tier: int):
        rows = await self.db.get_tier_roles(guild.id)
        roles = []
        for row in rows:
            if row["tier"] >= tier:
                role = guild.get_role(row["role_id"])
                if role:
                    roles.append(role)
        return roles


def require(key: str):
    """Command check: the caller's tier must meet whatever this command is currently set to."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            raise app_commands.NoPrivateMessage()
        pm = interaction.client.perms
        needed = await pm.required_tier(interaction.guild.id, key)
        if await pm.member_tier(interaction.user) >= needed:
            return True
        raise MissingTier(needed)

    return app_commands.check(predicate)


def require_tier(tier: int):
    """Command check with a fixed tier that can't be reconfigured."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            raise app_commands.NoPrivateMessage()
        if await interaction.client.perms.member_tier(interaction.user) >= tier:
            return True
        raise MissingTier(tier)

    return app_commands.check(predicate)
