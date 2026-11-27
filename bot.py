import logging
import os
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from db import Database
from perms import TIER_NAMES, MissingTier, PermissionManager

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("modbot")

EXTENSIONS = ("logs", "moderation", "autorole", "staff", "tickets")

# Hides each command from members who lack this Discord permission (staff still need the matching
# staff group to actually use it). Admins can show a command to more roles in
# Server Settings > Integrations > your bot.
COMMAND_VISIBILITY = {
    "warn": discord.Permissions(moderate_members=True),
    "history": discord.Permissions(moderate_members=True),
    "timeout": discord.Permissions(moderate_members=True),
    "untimeout": discord.Permissions(moderate_members=True),
    "ticket": discord.Permissions(moderate_members=True),
    "kick": discord.Permissions(kick_members=True),
    "ban": discord.Permissions(ban_members=True),
    "tempban": discord.Permissions(ban_members=True),
    "unban": discord.Permissions(ban_members=True),
    "purge": discord.Permissions(manage_messages=True),
    "removecase": discord.Permissions(manage_guild=True),
    "permissions": discord.Permissions(manage_guild=True),
    "setup_logs": discord.Permissions(manage_guild=True),
    "setlog": discord.Permissions(manage_guild=True),
    "autorole": discord.Permissions(manage_guild=True),
}

class ModBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True         
        intents.message_content = True  
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
        self.db = Database()
        self.perms = PermissionManager(self.db)
        self.tree.on_error = self.on_tree_error

    async def setup_hook(self):
        await self.db.connect()
        for ext in EXTENSIONS:
            await self.load_extension(ext)
            
        for command in self.tree.get_commands():
            visible_to = COMMAND_VISIBILITY.get(command.name)
            if visible_to is not None:
                command.default_permissions = visible_to

        
        dev_guild = (os.getenv("DEV_GUILD_ID") or "").strip()
        try:
            if dev_guild.isdigit():
                
                guild = discord.Object(id=int(dev_guild))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %d command(s) to dev guild %s", len(synced), dev_guild)
            else:
                
                synced = await self.tree.sync()
                log.info("Synced %d global command(s)", len(synced))
        except discord.HTTPException:
            log.exception(
                "Command sync failed. Check DEV_GUILD_ID and that the bot was invited with the applications.commands scope."
            )

    async def close(self):
        await self.db.close()
        await super().close()

    async def on_ready(self):
        log.info("Logged in as %s (%s) in %d guild(s)", self.user, self.user.id, len(self.guilds))

    async def send_log(
        self,
        guild: discord.Guild,
        log_type: str,
        embed: discord.Embed,
        file: Optional[discord.File] = None,
    ):
        """Send an embed to the channel configured for this log type (if any)."""
        channel_id = await self.db.get_log_channel(guild.id, log_type)
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            return
        try:
            if file:
                await channel.send(embed=embed, file=file)
            else:
                await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            log.exception("Couldn't send %s log in guild %s", log_type, guild.id)

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, MissingTier):
            msg = f"You need **{TIER_NAMES[error.needed]}** or higher to use that command."
        elif isinstance(error, app_commands.MissingPermissions):
            msg = "You don't have permission to use that command."
        elif isinstance(error, app_commands.BotMissingPermissions):
            perms = ", ".join(p.replace("_", " ") for p in error.missing_permissions)
            msg = f"I'm missing permissions I need for that: {perms}."
        elif isinstance(error, app_commands.NoPrivateMessage):
            msg = "That command only works in a server."
        elif isinstance(error, app_commands.CheckFailure):
            msg = "You can't use that command here."
        else:
            log.exception("Unhandled command error", exc_info=error)
            msg = "Something went wrong running that command."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is not set. Add it in Railway > Variables (or your .env file).")
    ModBot().run(token)


if __name__ == "__main__":
    main()
