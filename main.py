"""Safe Discord moderation bot: announcements, persistent panels and video proofs."""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ASC")

COLOR_SUCCESS = 0x57F287
COLOR_WARNING = 0xFEE75C
COLOR_DANGER = 0xED4245
COLOR_ORANGE = 0xE67E22

BYPASS_ROLE_ID = int(os.getenv("BYPASS_ROLE_ID", "1551706822216519800"))
VERIFIED_ROLE_ID = int(os.getenv("VERIFIED_ROLE_ID", "0"))
PROOF_CHANNEL_ID = int(os.getenv("PROOF_CHANNEL_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
PROOF_TIMEOUT_SECONDS = int(os.getenv("PROOF_TIMEOUT_SECONDS", "300"))
SETUP_FILE = Path("setup_data.json")
PROOF_FILE = Path("proof_data.json")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True


class ASCBot(commands.Bot):
    async def setup_hook(self):
        await self.load_extension("announcement")
        await self.tree.sync()
        log.info("Extension announcement chargee et commandes synchronisees.")


bot = ASCBot(command_prefix="!", intents=intents, help_command=None)
proof_requests: dict[str, dict[str, Any]] = {}
health_started = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as file:
                return json.load(file)
    except (OSError, json.JSONDecodeError):
        log.exception("Impossible de lire %s", path)
    return default


def save_json(path: Path, data: Any) -> None:
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)


def get_role(guild: discord.Guild, role_id: int) -> discord.Role | None:
    return guild.get_role(role_id) if role_id else None


async def send_log(embed: discord.Embed) -> None:
    if not LOG_CHANNEL_ID:
        return
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if isinstance(channel, discord.TextChannel):
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            log.exception("Impossible d'envoyer le log")


async def start_health_server() -> None:
    global health_started
    if health_started:
        return
    health_started = True
    app = web.Application()
    app.router.add_get("/", lambda request: web.Response(text="OK"))
    app.router.add_get("/health", lambda request: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    log.info("Health server actif sur le port %s", config.PORT)


def setup_view() -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    button = discord.ui.Button(
        label="✅ Demander une preuve",
        style=discord.ButtonStyle.success,
        custom_id="asc_proof_info",
    )

    async def callback(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "Un administrateur doit créer une demande avec `/proof_request`. "
            "Aucun code SMS ni donnée bancaire ne doit être envoyé.",
            ephemeral=True,
        )

    button.callback = callback
    view.add_item(button)
    return view


async def expire_proof(key: str) -> None:
    await asyncio.sleep(PROOF_TIMEOUT_SECONDS)
    request = proof_requests.pop(key, None)
    if not request:
        return
    channel = bot.get_channel(request["channel_id"])
    if isinstance(channel, discord.TextChannel):
        await channel.send(embed=discord.Embed(
            title="Délai expiré",
            description=f"<@{request['user_id']}> n'a pas envoyé de vidéo dans le délai imparti.",
            colour=COLOR_WARNING,
        ))
    await send_log(discord.Embed(
        title="Preuve expirée",
        description=f"Membre : <@{request['user_id']}> (`{request['user_id']}`)",
        colour=COLOR_WARNING,
    ))


@bot.tree.command(name="setupnsfw", description="Crée un panneau persistant de demande de preuve")
@app_commands.default_permissions(administrator=True)
async def setupnsfw(interaction: discord.Interaction):
    if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("Utilise cette commande dans un salon de serveur.", ephemeral=True)
        return

    embed = discord.Embed(
        title="Vérification du serveur",
        description="Utilise le bouton ci-dessous. Les preuves doivent être envoyées uniquement à la demande du staff.",
        colour=COLOR_ORANGE,
    )
    message = await interaction.channel.send(embed=embed, view=setup_view())
    entries = load_json(SETUP_FILE, [])
    if not isinstance(entries, list):
        entries = []
    entries = [entry for entry in entries if entry.get("channel_id") != interaction.channel.id]
    entries.append({
        "guild_id": interaction.guild.id,
        "channel_id": interaction.channel.id,
        "message_id": message.id,
    })
    save_json(SETUP_FILE, entries)
    await interaction.response.send_message("Panneau créé et sauvegardé.", ephemeral=True)


@bot.tree.command(name="bypass", description="Donne ou retire le rôle bypass")
@app_commands.default_permissions(administrator=True)
async def bypass(interaction: discord.Interaction, membre: discord.Member):
    role = get_role(interaction.guild, BYPASS_ROLE_ID) if interaction.guild else None
    if not role:
        await interaction.response.send_message("Rôle bypass introuvable. Vérifie BYPASS_ROLE_ID.", ephemeral=True)
        return
    try:
        if role in membre.roles:
            await membre.remove_roles(role, reason="Bypass retiré par un administrateur")
            message = f"Rôle bypass retiré à {membre.mention}."
        else:
            await membre.add_roles(role, reason="Bypass attribué par un administrateur")
            message = f"Rôle bypass attribué à {membre.mention}."
        await interaction.response.send_message(message, ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("Le rôle doit être placé sous le rôle du bot.", ephemeral=True)


@bot.tree.command(name="proof_request", description="Demande une preuve vidéo à un membre")
@app_commands.default_permissions(administrator=True)
async def proof_request(interaction: discord.Interaction, membre: discord.Member):
    if not interaction.guild:
        await interaction.response.send_message("Commande utilisable sur un serveur uniquement.", ephemeral=True)
        return
    bypass_role = get_role(interaction.guild, BYPASS_ROLE_ID)
    if bypass_role and bypass_role in membre.roles:
        await interaction.response.send_message("Ce membre possède le rôle bypass.", ephemeral=True)
        return

    channel_id = PROOF_CHANNEL_ID or interaction.channel_id
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message("Salon de preuve introuvable.", ephemeral=True)
        return

    key = f"{interaction.guild.id}:{membre.id}"
    if key in proof_requests:
        await interaction.response.send_message("Une demande est déjà active.", ephemeral=True)
        return

    proof_requests[key] = {
        "guild_id": interaction.guild.id,
        "user_id": membre.id,
        "channel_id": channel.id,
        "created_at": utc_now(),
    }
    embed = discord.Embed(
        title="Preuve vidéo demandée",
        description=(
            f"{membre.mention}, envoie une vidéo dans ce salon dans les 5 minutes.\n\n"
            "Seules les vidéos sont acceptées. Les photos et messages texte seront refusés."
        ),
        colour=COLOR_ORANGE,
        timestamp=discord.utils.utcnow(),
    )
    await channel.send(content=membre.mention, embed=embed)
    await interaction.response.send_message("Demande envoyée.", ephemeral=True)
    asyncio.create_task(expire_proof(key))


@bot.tree.command(name="sync", description="Synchronise les commandes slash")
@app_commands.default_permissions(administrator=True)
async def sync(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        synced = await bot.tree.sync()
        await interaction.followup.send(f"{len(synced)} commande(s) synchronisée(s).", ephemeral=True)
    except Exception as error:
        log.exception("Erreur de synchronisation")
        await interaction.followup.send(f"Erreur de synchronisation : {error}", ephemeral=True)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    key = f"{message.guild.id}:{message.author.id}"
    request = proof_requests.get(key)
    if not request or message.channel.id != request["channel_id"]:
        return

    videos = [attachment for attachment in message.attachments if (attachment.content_type or "").startswith("video/")]
    if not videos:
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        return

    proof_requests.pop(key, None)
    history = load_json(PROOF_FILE, [])
    if not isinstance(history, list):
        history = []
    history.append({
        "guild_id": message.guild.id,
        "user_id": message.author.id,
        "message_id": message.id,
        "channel_id": message.channel.id,
        "submitted_at": utc_now(),
    })
    save_json(PROOF_FILE, history[-1000:])

    role = get_role(message.guild, VERIFIED_ROLE_ID)
    if role:
        try:
            await message.author.add_roles(role, reason="Preuve vidéo reçue")
        except discord.Forbidden:
            log.warning("Impossible d'attribuer VERIFIED_ROLE_ID")

    await message.channel.send(embed=discord.Embed(
        title="Preuve reçue",
        description=f"La vidéo de {message.author.mention} a été reçue.",
        colour=COLOR_SUCCESS,
    ))
    await send_log(discord.Embed(
        title="Preuve vidéo reçue",
        description=f"Membre : {message.author.mention} (`{message.author.id}`)\nMessage : `{message.id}`",
        colour=COLOR_SUCCESS,
    ))


@bot.event
async def on_ready():
    log.info("Connecté : %s", bot.user)
    await start_health_server()
    for entry in load_json(SETUP_FILE, []):
        channel = bot.get_channel(entry.get("channel_id", 0))
        message_id = entry.get("message_id")
        if isinstance(channel, discord.TextChannel) and message_id:
            try:
                await channel.fetch_message(message_id)
                bot.add_view(setup_view(), message_id=message_id)
            except (discord.NotFound, discord.HTTPException):
                log.warning("Panneau introuvable : %s", message_id)


if __name__ == "__main__":
    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN manquant dans l'environnement")
    bot.run(config.BOT_TOKEN)
