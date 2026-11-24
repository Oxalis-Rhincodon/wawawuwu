import os
import time
from typing import Optional

import aiosqlite


DB_PATH = os.getenv("DB_PATH") or os.path.join(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "."), "modbot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS infractions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    action       TEXT    NOT NULL,
    reason       TEXT    NOT NULL,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER,
    active       INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_infractions_user ON infractions (guild_id, user_id);
CREATE INDEX IF NOT EXISTS idx_infractions_expiry ON infractions (action, active, expires_at);

CREATE TABLE IF NOT EXISTS log_channels (
    guild_id   INTEGER NOT NULL,
    log_type   TEXT    NOT NULL,
    channel_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, log_type)
);

CREATE TABLE IF NOT EXISTS autoroles (
    guild_id INTEGER PRIMARY KEY,
    role_id  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tier_roles (
    guild_id INTEGER NOT NULL,
    role_id  INTEGER NOT NULL,
    tier     INTEGER NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

CREATE TABLE IF NOT EXISTS command_perms (
    guild_id INTEGER NOT NULL,
    command  TEXT    NOT NULL,
    tier     INTEGER NOT NULL,
    PRIMARY KEY (guild_id, command)
);

CREATE TABLE IF NOT EXISTS ticket_config (
    guild_id     INTEGER PRIMARY KEY,
    category_id  INTEGER NOT NULL,
    ping_role_id INTEGER
);

CREATE TABLE IF NOT EXISTS tickets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    number       INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    type         TEXT    NOT NULL,
    subject      TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'open',
    claimed_by   INTEGER,
    created_at   INTEGER NOT NULL,
    closed_at    INTEGER,
    closed_by    INTEGER,
    close_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_channel ON tickets (channel_id);
CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets (guild_id, user_id, status);
"""


class Database:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def _one(self, sql: str, params=()):
        cur = await self.conn.execute(sql, params)
        return await cur.fetchone()

    async def _all(self, sql: str, params=()):
        cur = await self.conn.execute(sql, params)
        return await cur.fetchall()

    async def _run(self, sql: str, params=()):
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur

    # ---------------------------------------------------------- log channels
    async def set_log_channel(self, guild_id: int, log_type: str, channel_id: int):
        await self._run(
            "INSERT INTO log_channels (guild_id, log_type, channel_id) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, log_type) DO UPDATE SET channel_id = excluded.channel_id",
            (guild_id, log_type, channel_id),
        )

    async def get_log_channel(self, guild_id: int, log_type: str) -> Optional[int]:
        row = await self._one(
            "SELECT channel_id FROM log_channels WHERE guild_id = ? AND log_type = ?", (guild_id, log_type)
        )
        return row["channel_id"] if row else None

    # ----------------------------------------------------------- infractions
    async def add_infraction(self, guild_id, user_id, moderator_id, action, reason, expires_at=None) -> int:
        cur = await self._run(
            "INSERT INTO infractions (guild_id, user_id, moderator_id, action, reason, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, moderator_id, action, reason, int(time.time()), expires_at),
        )
        return cur.lastrowid

    async def get_infractions(self, guild_id: int, user_id: int):
        return await self._all(
            "SELECT * FROM infractions WHERE guild_id = ? AND user_id = ? ORDER BY id DESC", (guild_id, user_id)
        )

    async def get_infraction(self, guild_id: int, case_id: int):
        return await self._one("SELECT * FROM infractions WHERE guild_id = ? AND id = ?", (guild_id, case_id))

    async def count_infractions(self, guild_id: int, user_id: int, action: str) -> int:
        row = await self._one(
            "SELECT COUNT(*) AS c FROM infractions WHERE guild_id = ? AND user_id = ? AND action = ?",
            (guild_id, user_id, action),
        )
        return row["c"]

    async def delete_infraction(self, guild_id: int, case_id: int):
        await self._run("DELETE FROM infractions WHERE guild_id = ? AND id = ?", (guild_id, case_id))

    # -------------------------------------------------------------- tempbans
    async def get_expired_tempbans(self, now: int):
        return await self._all(
            "SELECT * FROM infractions WHERE action = 'tempban' AND active = 1 "
            "AND expires_at IS NOT NULL AND expires_at <= ?",
            (now,),
        )

    async def deactivate(self, case_id: int):
        await self._run("UPDATE infractions SET active = 0 WHERE id = ?", (case_id,))

    async def deactivate_tempbans(self, guild_id: int, user_id: int):
        await self._run(
            "UPDATE infractions SET active = 0 "
            "WHERE guild_id = ? AND user_id = ? AND action = 'tempban' AND active = 1",
            (guild_id, user_id),
        )

    # -------------------------------------------------------------- autorole
    async def get_autorole(self, guild_id: int) -> Optional[int]:
        row = await self._one("SELECT role_id FROM autoroles WHERE guild_id = ?", (guild_id,))
        return row["role_id"] if row else None

    async def set_autorole(self, guild_id: int, role_id: int):
        await self._run(
            "INSERT INTO autoroles (guild_id, role_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET role_id = excluded.role_id",
            (guild_id, role_id),
        )

    async def clear_autorole(self, guild_id: int):
        await self._run("DELETE FROM autoroles WHERE guild_id = ?", (guild_id,))

    # ------------------------------------------------------ permission tiers
    async def get_tier_roles(self, guild_id: int):
        return await self._all("SELECT role_id, tier FROM tier_roles WHERE guild_id = ?", (guild_id,))

    async def set_tier_role(self, guild_id: int, role_id: int, tier: int):
        await self._run(
            "INSERT INTO tier_roles (guild_id, role_id, tier) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, role_id) DO UPDATE SET tier = excluded.tier",
            (guild_id, role_id, tier),
        )

    async def remove_tier_role(self, guild_id: int, role_id: int) -> bool:
        cur = await self._run("DELETE FROM tier_roles WHERE guild_id = ? AND role_id = ?", (guild_id, role_id))
        return cur.rowcount > 0

    async def get_command_tier(self, guild_id: int, command: str) -> Optional[int]:
        row = await self._one(
            "SELECT tier FROM command_perms WHERE guild_id = ? AND command = ?", (guild_id, command)
        )
        return row["tier"] if row else None

    async def set_command_tier(self, guild_id: int, command: str, tier: int):
        await self._run(
            "INSERT INTO command_perms (guild_id, command, tier) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, command) DO UPDATE SET tier = excluded.tier",
            (guild_id, command, tier),
        )

    async def clear_command_tier(self, guild_id: int, command: str):
        await self._run("DELETE FROM command_perms WHERE guild_id = ? AND command = ?", (guild_id, command))

    # --------------------------------------------------------------- tickets
    async def set_ticket_config(self, guild_id: int, category_id: int, ping_role_id: Optional[int]):
        await self._run(
            "INSERT INTO ticket_config (guild_id, category_id, ping_role_id) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET category_id = excluded.category_id, "
            "ping_role_id = excluded.ping_role_id",
            (guild_id, category_id, ping_role_id),
        )

    async def get_ticket_config(self, guild_id: int):
        return await self._one("SELECT * FROM ticket_config WHERE guild_id = ?", (guild_id,))

    async def next_ticket_number(self, guild_id: int) -> int:
        row = await self._one(
            "SELECT COALESCE(MAX(number), 0) + 1 AS n FROM tickets WHERE guild_id = ?", (guild_id,)
        )
        return row["n"]

    async def create_ticket(self, guild_id, number, channel_id, user_id, ticket_type, subject) -> int:
        cur = await self._run(
            "INSERT INTO tickets (guild_id, number, channel_id, user_id, type, subject, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (guild_id, number, channel_id, user_id, ticket_type, subject, int(time.time())),
        )
        return cur.lastrowid

    async def get_open_ticket(self, channel_id: int):
        return await self._one(
            "SELECT * FROM tickets WHERE channel_id = ? AND status = 'open'", (channel_id,)
        )

    async def list_open_tickets(self, guild_id: int, user_id: Optional[int] = None):
        if user_id is None:
            return await self._all(
                "SELECT * FROM tickets WHERE guild_id = ? AND status = 'open' ORDER BY id", (guild_id,)
            )
        return await self._all(
            "SELECT * FROM tickets WHERE guild_id = ? AND user_id = ? AND status = 'open' ORDER BY id",
            (guild_id, user_id),
        )

    async def set_ticket_claimed(self, ticket_id: int, user_id: int):
        await self._run("UPDATE tickets SET claimed_by = ? WHERE id = ?", (user_id, ticket_id))

    async def close_ticket(self, ticket_id: int, closed_by: int, reason: str):
        await self._run(
            "UPDATE tickets SET status = 'closed', closed_at = ?, closed_by = ?, close_reason = ? WHERE id = ?",
            (int(time.time()), closed_by, reason, ticket_id),
        )

    async def close_ticket_for_deleted_channel(self, channel_id: int):
        await self._run(
            "UPDATE tickets SET status = 'closed', closed_at = ?, close_reason = 'Channel was deleted' "
            "WHERE channel_id = ? AND status = 'open'",
            (int(time.time()), channel_id),
        )
