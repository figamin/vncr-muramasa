import discord
from discord import app_commands
from discord.ext import commands
from typing import Optional
import logging

logger = logging.getLogger(__name__)

CONFIRMED_READER_ROLE = "Confirmed Reader"
CONFIG_KEY = "self_assignable_roles"
MAX_MENU_ROLES = 25  # Discord select component hard limit
MAX_DESCRIPTION_LEN = 100  # Discord SelectOption.description hard limit
MENU_TIMEOUT = 180


def compute_role_diff(current_ids, offered_ids, selected_ids):
    """Roles to add and remove for a /roles submission.

    Only roles in `offered_ids` are ever touched, so a submitted value outside
    the menu can't grant an unrelated role, and roles the user holds outside the
    menu are left alone. Returns (to_add, to_remove) as id sets.
    """
    offered = set(offered_ids)
    current = set(current_ids)
    selected = set(selected_ids) & offered
    to_add = selected - current
    to_remove = (offered & current) - selected
    return to_add, to_remove


def validation_error_for_add(*, role_id, role_position, role_managed, role_is_default,
                             bot_top_position, existing_ids, max_options=MAX_MENU_ROLES):
    """User-facing reason a role can't join the menu, or None if it's fine."""
    if role_is_default:
        return "That's the @everyone role, which can't be a menu option."
    if role_managed:
        return "That role is managed by an integration and can't be self-assigned."
    if role_id in existing_ids:
        return "That role is already in the menu."
    if role_position >= bot_top_position:
        return ("That role sits at or above my own top role, so I can't assign it. "
                "Move my role above it and try again.")
    if len(existing_ids) >= max_options:
        return f"The menu is full ({max_options} roles max). Remove one first."
    return None


def normalize_role_entry(raw):
    """Coerce a stored menu entry into a dict, or None if unparseable.

    Tolerates the legacy format (a bare role-id string/int) alongside the
    current dict form so old configs keep working until the next write.
    Returns {"id": int, "emoji": str|None, "description": str|None}.
    """
    if isinstance(raw, dict):
        rid, emoji, description = raw.get("id"), raw.get("emoji"), raw.get("description")
    else:
        rid, emoji, description = raw, None, None
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        return None
    return {"id": rid, "emoji": emoji or None, "description": description or None}


def validate_option_meta(emoji, description):
    """User-facing reason the emoji/description can't be used, or None."""
    if description is not None and len(description) > MAX_DESCRIPTION_LEN:
        return (f"Description is too long ({len(description)}/{MAX_DESCRIPTION_LEN} "
                "characters). Shorten it.")
    if emoji:
        e = emoji.strip()
        # Custom emoji (<:name:id> / <a:name:id>) is verified against the guild by
        # the command. Otherwise reject plain ASCII so a typo can't break the menu.
        if e and not (e.startswith("<") and e.endswith(">")) and all(ord(c) < 128 for c in e):
            return "That doesn't look like an emoji. Use a standard emoji (e.g. 🎬) or a custom server emoji."
    return None


def parse_emoji(emoji):
    """Best-effort PartialEmoji for a stored emoji string, or None."""
    if not emoji:
        return None
    try:
        return discord.PartialEmoji.from_str(emoji.strip())
    except Exception:
        return None


class RoleSelect(discord.ui.Select):
    def __init__(self, member: discord.Member, offered):
        # offered: list of (role, emoji, description)
        self._offered = {role.id: role for role, _, _ in offered}
        member_role_ids = {role.id for role in member.roles}
        options = []
        for role, emoji, description in offered:
            kwargs = {
                "label": role.name[:100],
                "value": str(role.id),
                "default": role.id in member_role_ids,
            }
            if description:
                kwargs["description"] = description[:MAX_DESCRIPTION_LEN]
            pe = parse_emoji(emoji)
            if pe is not None:
                kwargs["emoji"] = pe
            options.append(discord.SelectOption(**kwargs))
        super().__init__(
            placeholder="Select your roles…",
            min_values=0,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        member = interaction.user
        offered_ids = set(self._offered)
        current_ids = {role.id for role in member.roles}
        selected_ids = {int(value) for value in self.values}
        to_add, to_remove = compute_role_diff(current_ids, offered_ids, selected_ids)

        if not to_add and not to_remove:
            await interaction.response.edit_message(
                content="No changes. Your roles are already set that way.", view=None)
            return

        await interaction.response.defer()
        add_roles = [self._offered[rid] for rid in to_add]
        remove_roles = [self._offered[rid] for rid in to_remove]
        try:
            if add_roles:
                await member.add_roles(*add_roles, reason="Self-assigned via /roles")
            if remove_roles:
                await member.remove_roles(*remove_roles, reason="Self-removed via /roles")
        except discord.Forbidden:
            await interaction.edit_original_response(
                content=("❌ I couldn't update your roles. I need the Manage Roles "
                         "permission and my role must sit above the ones you picked."),
                view=None)
            return

        parts = []
        if add_roles:
            parts.append("**Added:** " + ", ".join(role.name for role in add_roles))
        if remove_roles:
            parts.append("**Removed:** " + ", ".join(role.name for role in remove_roles))
        await interaction.edit_original_response(
            content="✅ Updated your roles.\n" + "\n".join(parts), view=None)


class RoleSelectView(discord.ui.View):
    def __init__(self, member: discord.Member, offered):
        super().__init__(timeout=MENU_TIMEOUT)
        self.author_id = member.id
        self.add_item(RoleSelect(member, offered))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This menu isn't yours. Run `/roles` to get your own.", ephemeral=True)
            return False
        return True


class RoleMenuCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _guild_conf(self, guild_id) -> dict:
        return self.bot.config.setdefault("guilds", {}).setdefault(str(guild_id), {})

    def _get_role_entries(self, guild_id) -> list:
        entries = []
        for raw in self._guild_conf(guild_id).get(CONFIG_KEY, []):
            entry = normalize_role_entry(raw)
            if entry is not None:
                entries.append(entry)
        return entries

    def _set_role_entries(self, guild_id, entries) -> None:
        self._guild_conf(guild_id)[CONFIG_KEY] = [
            {"id": str(e["id"]), "emoji": e["emoji"], "description": e["description"]}
            for e in entries
        ]
        self.bot.save_config()

    def _resolve_entries(self, guild: discord.Guild) -> list:
        """Live (role, emoji, description) tuples, pruning entries whose role is gone."""
        entries = self._get_role_entries(guild.id)
        resolved, alive = [], []
        for entry in entries:
            role = guild.get_role(entry["id"])
            if role is not None:
                resolved.append((role, entry["emoji"], entry["description"]))
                alive.append(entry)
        if alive != entries:
            self._set_role_entries(guild.id, alive)
        return resolved

    @staticmethod
    def _is_confirmed_reader(member: discord.Member) -> bool:
        return any(role.name == CONFIRMED_READER_ROLE for role in member.roles)

    @app_commands.command(name="roles", description="Pick your optional roles")
    @app_commands.guild_only()
    async def roles(self, interaction: discord.Interaction):
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True)
            return
        if not self._is_confirmed_reader(member):
            await interaction.response.send_message(
                "🔒 Only Confirmed Readers can pick optional roles.", ephemeral=True)
            return
        offered = self._resolve_entries(member.guild)
        if not offered:
            await interaction.response.send_message(
                "No optional roles are available yet.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Tick the roles you want, untick the ones you don't:",
            view=RoleSelectView(member, offered), ephemeral=True)

    @app_commands.command(
        name="manage_roles",
        description="[ADMIN] Add, remove, or list the optional roles offered by /roles",
    )
    @app_commands.describe(
        action="What to do with the /roles menu.",
        role="Role to add or remove (not needed for list).",
        emoji="(add) Emoji shown beside the role in the menu.",
        description="(add) Short note shown under the role in the menu.",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="add", value="add"),
        app_commands.Choice(name="remove", value="remove"),
        app_commands.Choice(name="list", value="list"),
    ])
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.guild_only()
    async def manage_roles(self, interaction: discord.Interaction,
                           action: str,
                           role: Optional[discord.Role] = None,
                           emoji: Optional[str] = None,
                           description: Optional[str] = None):
        if action == "list":
            await self._manage_list(interaction)
        elif role is None:
            await interaction.response.send_message(
                f"❌ Pick a role to {action}.", ephemeral=True)
        elif action == "add":
            await self._manage_add(interaction, role, emoji, description)
        else:  # remove
            await self._manage_remove(interaction, role)

    async def _manage_add(self, interaction: discord.Interaction, role: discord.Role,
                          emoji: Optional[str], description: Optional[str]):
        meta_error = validate_option_meta(emoji, description)
        if meta_error:
            await interaction.response.send_message(f"❌ {meta_error}", ephemeral=True)
            return
        parsed = parse_emoji(emoji)
        if parsed is not None and parsed.id is not None and interaction.guild.get_emoji(parsed.id) is None:
            await interaction.response.send_message(
                "❌ I can't use that custom emoji. It must be from this server.", ephemeral=True)
            return

        emoji_clean = emoji.strip() if emoji else None
        desc_clean = description.strip() if description else None
        entries = self._get_role_entries(interaction.guild_id)
        existing = next((e for e in entries if e["id"] == role.id), None)
        if existing is not None:
            existing["emoji"] = emoji_clean
            existing["description"] = desc_clean
            self._set_role_entries(interaction.guild_id, entries)
            await interaction.response.send_message(
                f"✅ Updated **{role.name}** in the /roles menu.", ephemeral=True)
            return

        error = validation_error_for_add(
            role_id=role.id,
            role_position=role.position,
            role_managed=role.managed,
            role_is_default=role.is_default(),
            bot_top_position=interaction.guild.me.top_role.position,
            existing_ids={e["id"] for e in entries},
            max_options=MAX_MENU_ROLES,
        )
        if error:
            await interaction.response.send_message(f"❌ {error}", ephemeral=True)
            return
        entries.append({"id": role.id, "emoji": emoji_clean, "description": desc_clean})
        self._set_role_entries(interaction.guild_id, entries)
        label = f"{emoji_clean} **{role.name}**" if emoji_clean else f"**{role.name}**"
        await interaction.response.send_message(
            f"✅ Added {label} to the /roles menu.", ephemeral=True)

    async def _manage_remove(self, interaction: discord.Interaction, role: discord.Role):
        entries = self._get_role_entries(interaction.guild_id)
        if not any(e["id"] == role.id for e in entries):
            await interaction.response.send_message(
                f"❌ **{role.name}** isn't in the /roles menu.", ephemeral=True)
            return
        entries = [e for e in entries if e["id"] != role.id]
        self._set_role_entries(interaction.guild_id, entries)
        await interaction.response.send_message(
            f"✅ Removed **{role.name}** from the /roles menu. The role itself was not deleted.",
            ephemeral=True)

    async def _manage_list(self, interaction: discord.Interaction):
        offered = self._resolve_entries(interaction.guild)
        if not offered:
            await interaction.response.send_message(
                "No optional roles are configured. Add some with `/manage_roles action:add`.",
                ephemeral=True)
            return
        lines = []
        for role, emoji, description in offered:
            prefix = f"{emoji} " if emoji else ""
            suffix = f": {description}" if description else ""
            lines.append(f"• {prefix}{role.mention}{suffix}")
        await interaction.response.send_message(
            f"**Optional roles in /roles ({len(offered)}/{MAX_MENU_ROLES}):**\n" + "\n".join(lines),
            ephemeral=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            msg = "You need the Manage Roles permission to use this command."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        else:
            raise error


async def setup(bot):
    await bot.add_cog(RoleMenuCog(bot))
