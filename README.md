# ModBot — complete feature guide

A Discord moderation bot for one or many servers. Everything is a **slash command**. It logs nearly everything to **separate channels**, tracks every offense in a database, runs a **ticket system**, hands out an **autorole**, and lets you decide which roles can do what using three staff groups: **Trial Mod**, **Mod** and **Admin**.

---

## 1. Staff permission groups

Assign any of your server's roles to one of three groups with `/permissions addrole`. A person's group is the highest one among their roles. **Each group can do everything the groups below it can.** The server owner and anyone with Discord's *Administrator* permission always count as Admin, so you can't lock yourself out.

| Group | Default abilities |
|---|---|
| **Trial Mod** | `/warn`, `/history`, `/timeout` (up to **1 hour**), `/untimeout`, see and handle tickets |
| **Mod** | everything above, plus `/kick`, `/ban`, `/tempban`, `/unban`, `/purge`, and timeouts up to **7 days**. Can read the Mod Logs category |
| **Admin** | everything above, plus `/removecase`, timeouts up to **28 days**, and all setup commands |

**Always Admin-only (can't be changed):** `/permissions`, `/setup_logs`, `/setlog`, `/autorole`, `/ticket setup`, `/ticket panel`.

**Fully adjustable:** with `/permissions command` you can change the group needed for `/warn`, `/history`, `/timeout`, `/untimeout`, `/kick`, `/ban`, `/tempban`, `/unban`, `/purge`, `/removecase` and ticket staff access. For example, let Trial Mods use `/kick`, or restrict `/purge` to Admins. Pick "Reset to default" to undo a change.

**Staff protection:** you can't use `/warn`, `/timeout`, `/kick`, `/ban` or `/tempban` on someone in a staff group at or above your own. Only the server owner can act on admins. Nobody can act on the owner or on themselves.

| Command | What it does |
|---|---|
| `/permissions addrole tier role` | Put a role in Trial Mod, Mod or Admin (a role can only be in one group) |
| `/permissions removerole role` | Take a role out of its group |
| `/permissions command command tier` | Change which group a command needs |
| `/permissions list` | Show every group, its roles, what it unlocks and its timeout limit |
| `/permissions mytier` | Anyone can check which group they're in |

---

## 2. Moderation commands

| Command | What it does |
|---|---|
| `/warn member reason` | Records a warning, DMs the member, and shows their total warnings |
| `/timeout member duration [reason]` | Mutes a member for e.g. `30m`, `2h`, `1d12h`. The longest allowed depends on your group |
| `/untimeout member [reason]` | Ends a timeout early |
| `/kick member [reason]` | Kicks a member (they get a DM first) |
| `/ban user [reason] [delete_days]` | Permanent ban. Works on people who already left. Optionally deletes 0 to 7 days of their messages |
| `/tempban user duration [reason] [delete_days]` | Ban that lifts itself after `12h`, `3d`, `1w`, etc. |
| `/unban user_id [reason]` | Unbans by ID and cancels any pending tempban |
| `/purge amount [user] [reason]` | Scans the last 1 to 500 messages here and deletes them, optionally only one user's. Pinned messages are skipped. A text transcript of the deleted messages goes to the message log |
| `/history user` | A user's past offenses: case numbers, actions, moderators, dates and reasons (10 most recent shown, with totals by type) |
| `/removecase case_id` | Deletes a case from someone's record. The removal is logged |

Details worth knowing:
- Every warn, timeout, kick, ban and tempban becomes a numbered **case** stored in the database.
- Members are **DMed** about the action when possible, and the log records whether the DM went through.
- **Tempbans survive restarts.** A background check runs every 30 seconds and unbans anyone whose time is up, even if the bot was offline when it expired.
- Duration format: combine `w`, `d`, `h`, `m`, `s`, like `1w2d` or `90m`.
- Moderators' reasons are written to Discord's audit log too, with the moderator's name.

---

## 3. Logging: one channel per type

Run `/setup_logs` once. It creates a private **Mod Logs** category (readable by admins and the Mod and Admin groups) with a separate channel for each type below. Run it again any time and it only creates what's missing. Use `/setlog` to send a type to a channel you already have.

| Channel | What goes in it |
|---|---|
| `join-leave-logs` | Joins (account age, warning for accounts under 7 days old, member count) and leaves (join date, roles they had) |
| `member-logs` | Nickname changes, role added/removed, timeouts applied/removed, username/display name/avatar changes, autoroles given |
| `message-logs` | Edits (before and after), deletions (content, author, attachment links), bulk deletes with a transcript file |
| `voice-logs` | Joining, leaving and moving voice channels, server and self mute/deafen, camera and streaming |
| `channel-logs` | Channels and threads created, deleted, and edited (name, topic, slowmode, NSFW, bitrate, user limit, category, permission overwrites) |
| `role-logs` | Roles created, deleted, and edited (name, color, hoist, mentionable, permissions added/removed) |
| `server-logs` | Server name, icon, owner and verification level changes, emoji changes, invites created and deleted |
| `mod-logs` | Every case from the commands above, tempban expirations, purges, removed cases, and bans/unbans done by hand outside the bot (with who did it, when the bot can see the audit log) |
| `ticket-logs` | Tickets opened, claimed, closed, with full transcripts |
| `command-logs` | Every slash command used: who, where, and with which options |

Bots' own messages are not logged. Message content can only be shown for messages the bot has seen since it last started.

---

## 4. Ticket system

Members open private tickets with a button. Staff handle them. Everything is transcribed.

**Setup (Admins, once):**
1. Make a category (folder) for tickets, e.g. "Tickets".
2. `/ticket setup category [ping_role]`: tells the bot where to create tickets and optionally which role to ping.
3. `/ticket panel [channel] [title] [description]`: posts the panel with three buttons: **🎫 Support**, **🚨 Report**, **⚖️ Appeal**.
4. Run `/setup_logs` if you haven't, so closed tickets and transcripts have somewhere to go.

**When a member presses a button:**
- They fill in a short form (subject and details).
- The bot creates a private channel like `support-0007`, visible only to that member, the bot, and your staff groups. Which groups count as staff is the "ticket staff access" setting (Trial Mod and up by default).
- The first message shows who opened it and why, pings the ping role, and has **Claim** and **Close** buttons.
- Each member can have up to **2 open tickets** at a time.

**Handling tickets:**

| Command | Who | What it does |
|---|---|---|
| Claim button or `/ticket claim` | Staff | Marks you as handling it. Only admins can take over a ticket someone else claimed |
| Close button or `/ticket close [reason]` | Opener or staff | Saves a transcript, posts it to `ticket-logs`, DMs the opener a copy (if their DMs are open), then deletes the channel after 5 seconds |
| `/ticket add member` | Staff | Gives someone access to this ticket |
| `/ticket remove member` | Staff | Removes someone (not the opener) |
| `/ticket rename name` | Staff | Renames the channel |
| `/ticket list [user]` | Staff | Lists open tickets with their opener, age and who claimed them |

Panel and ticket buttons keep working after the bot restarts. If a ticket channel is deleted by hand, the opener's ticket slot is freed automatically. Transcripts include up to the last 1000 messages.

---

## 5. Autorole

| Command | What it does |
|---|---|
| `/autorole set role` | Every new member gets this role when they join |
| `/autorole view` | Shows the current autorole |
| `/autorole clear` | Turns it off |

The setting is stored in the database and each assignment is logged in `member-logs`. The bot refuses roles that are managed by integrations, @everyone, or above the bot's own top role. It also refuses roles above the top role of whoever sets it.

---

## 6. Setting it up

**Discord developer portal**
1. Create an application, add a bot, copy the token.
2. Under Bot, turn on **Server Members Intent** and **Message Content Intent**.
3. Invite it with this link (replace `YOUR_CLIENT_ID`). It includes the `bot` and `applications.commands` scopes and these permissions: View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Manage Messages, Manage Channels, Manage Roles, Kick Members, Ban Members, Moderate Members, View Audit Log.

```
https://discord.com/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=1099780189334&scope=bot%20applications.commands
```

4. In your server, drag the bot's role **above** the roles it needs to manage (members it moderates, the autorole, and so on). Discord only lets a bot act on roles lower than its own.

**Railway**
1. Put all files in this folder at the **top level** of a GitHub repo (no subfolders).
2. In Railway: New Project, then Deploy from GitHub repo.
3. Add a **Volume** to the service, mounted at `/data`. The bot detects it and stores its database there automatically, so cases, tickets, permissions and settings survive restarts.
4. In Variables, add `DISCORD_TOKEN`. Optionally add `DEV_GUILD_ID` (just the server's ID) for instant command sync while testing.
5. Deploy and look for **Logged in as ...** and **Synced N command(s)** in the logs.

**First steps in your server**
1. `/setup_logs`
2. `/permissions addrole` for each of your staff roles
3. `/ticket setup` then `/ticket panel`
4. `/autorole set` if you want one

---

## 7. Troubleshooting

- **Slash commands don't appear:** re-invite with the `applications.commands` scope, check `DEV_GUILD_ID` matches your server (or delete it), then refresh Discord with Ctrl+R. Global commands can take a while the first time.
- **"You need Mod or higher":** your role isn't in a staff group yet. An admin should run `/permissions addrole`.
- **"I'm missing permissions":** check the invite permissions above, and that the bot's role sits above the role or member in question.
- **Tickets can't be created:** run `/ticket setup` and make sure the bot has Manage Channels.
- **History or settings vanish after a redeploy:** the Railway Volume isn't attached.
- **ModuleNotFoundError:** every `.py` file must be at the top level of the repo.
