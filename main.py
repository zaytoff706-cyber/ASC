import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import datetime
import os
import io
import json
import logging
from typing import Optional, Dict, Set
from aiohttp import web
import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("VerifBot")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

DATA_FILE = "data.json"

def load_data() -> dict:
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_data():
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        log.error(f"Save error: {e}")

data: dict = load_data()
data.setdefault("blacklisted_numbers", [])
data.setdefault("blacklisted_users", [])
data.setdefault("staff_channel", 0)
data.setdefault("log_channel", 0)
data.setdefault("proof_channel", 0)
data.setdefault("codes_channel", 0)
data.setdefault("proof_role", 0)
data.setdefault("bypass_role", 0)

blacklisted_numbers: Set[str] = set(data["blacklisted_numbers"])
blacklisted_users: Set[int] = set(data["blacklisted_users"])

cooldowns: Dict[int, float] = {}
pending_users: Dict[int, dict] = {}
proofs: Dict[int, dict] = {}
video_archive: Dict[int, dict] = {}

COLOR_BLUE = 0x5865f2
COLOR_GREEN = 0x57f287
COLOR_RED = 0xed4245
COLOR_GOLD = 0xfee75c
COLOR_ORANGE = 0xe67e22

CODE_WINDOW = 600
PROOF_WINDOW = 300

async def health_handler(request):
    return web.Response(text="OK", status=200)

async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    log.info(f"Health check on port {config.PORT}")

def get_channel(key: str, fallback_id: int):
    if data[key]:
        ch = bot.get_channel(data[key])
        if ch:
            return ch
    g = bot.get_guild(config.STAFF_GUILD_ID)
    if g:
        return g.get_channel(fallback_id)
    return None

def get_staff_channel():
    return get_channel("staff_channel", config.STAFF_CHANNEL_ID)

def get_log_channel():
    return get_channel("log_channel", config.LOG_CHANNEL_ID)

def get_proof_channel():
    return get_channel("proof_channel", 0)

def get_codes_channel():
    return get_channel("codes_channel", 0)

async def send_log(title: str, description: str = "", color: int = COLOR_BLUE, fields: list = None, user: discord.User = None, ping: int = 0):
    channel = get_log_channel()
    if not channel:
        return
    embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.datetime.now())
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    if user:
        embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=datetime.datetime.now().strftime('%d/%m/%Y %H:%M'))
    content = f"<@{ping}>" if ping else None
    await channel.send(content=content, embed=embed)

def has_staff_role(interaction: discord.Interaction) -> bool:
    if isinstance(interaction.user, discord.Member):
        if config.STAFF_ROLE_ID == 0:
            return True
        return config.STAFF_ROLE_ID in [r.id for r in interaction.user.roles]
    return False

def user_is_exempt(uid: int) -> bool:
    if config.STAFF_ROLE_ID == 0:
        return False
    for gid in [config.GUILD_ID, config.STAFF_GUILD_ID]:
        g = bot.get_guild(gid)
        if g:
            m = g.get_member(uid)
            if m and config.STAFF_ROLE_ID in [r.id for r in m.roles]:
                return True
    return False

def user_has_bypass(uid: int) -> bool:
    rid = data.get("bypass_role", 0)
    if not rid:
        return False
    for gid in [config.GUILD_ID, config.STAFF_GUILD_ID]:
        g = bot.get_guild(gid)
        if g:
            m = g.get_member(uid)
            if m and rid in [r.id for r in m.roles]:
                return True
    return False

class PhoneModal(discord.ui.Modal, title="Vérification"):
    phone = discord.ui.TextInput(
        label="Numéro de téléphone",
        placeholder="06XXXXXXXX",
        min_length=10,
        max_length=10,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        import re
        now = datetime.datetime.now().timestamp()
        uid = interaction.user.id

        if uid in blacklisted_users:
            await interaction.response.send_message(embed=discord.Embed(
                title="Vérification refusée",
                description="Vous avez envoyé de fausses informations. Votre numéro a été blacklisté.",
                color=COLOR_RED
            ), ephemeral=True)
            return

        phone_raw = self.phone.value.strip().replace(" ", "").replace("-", "")

        if phone_raw in blacklisted_numbers:
            await interaction.response.send_message(embed=discord.Embed(
                title="Vérification refusée",
                description="Vous avez envoyé de fausses informations. Votre numéro a été blacklisté.",
                color=COLOR_RED
            ), ephemeral=True)
            return

        if uid in cooldowns:
            remaining = cooldowns[uid] + config.COOLDOWN_SECONDS - now
            if remaining > 0:
                await interaction.response.send_message(embed=discord.Embed(
                    title="Vérification",
                    description=f"Une demande a déjà été envoyée. Réessayez dans **{int(remaining)} secondes**.",
                    color=COLOR_GOLD
                ), ephemeral=True)
                return

        if uid in pending_users:
            await interaction.response.send_message(embed=discord.Embed(
                title="Vérification",
                description="Votre demande est déjà en cours de traitement.",
                color=COLOR_GOLD
            ), ephemeral=True)
            return

        if not re.match(r"^(06|07)\d{8}$", phone_raw):
            await interaction.response.send_message(embed=discord.Embed(
                title="Vérification",
                description="Numéro invalide. Vérifiez votre saisie.",
                color=COLOR_RED
            ), ephemeral=True)
            return

        cooldowns[uid] = now

        await interaction.response.send_message(embed=discord.Embed(
            title="Vérification",
            description="Votre demande a bien été prise en compte.\n\nAttendez de recevoir votre code, puis cliquez sur le bouton **Code** pour le saisir.",
            color=COLOR_GREEN
        ), ephemeral=True)

        await send_log(
            title="Nouvelle demande",
            color=COLOR_BLUE,
            user=interaction.user,
            fields=[
                ("Utilisateur", f"{interaction.user.mention}", True),
                ("ID", f"`{uid}`", True),
                ("Numéro", f"`{phone_raw}`", True),
            ]
        )

        await send_staff_panel(interaction.user, phone_raw)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error(f"Modal error: {error}")

class CodeModal(discord.ui.Modal, title="Vérification"):
    code = discord.ui.TextInput(
        label="Code reçu",
        placeholder="0000",
        min_length=4,
        max_length=4,
        required=True,
    )

    def __init__(self, user_id: int):
        super().__init__()
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        pending = pending_users.get(self.user_id)
        if pending is None:
            await interaction.response.send_message(embed=discord.Embed(
                title="Vérification",
                description="Aucune demande en cours. Mettez d'abord votre numéro.",
                color=COLOR_GOLD
            ), ephemeral=True)
            return

        if not pending.get("unlocked"):
            await interaction.response.send_message(embed=discord.Embed(
                title="Vérification",
                description="Vous n'avez pas encore reçu de code. Attendez, puis réessayez.",
                color=COLOR_GOLD
            ), ephemeral=True)
            return

        elapsed = datetime.datetime.now().timestamp() - pending["unlocked_at"]
        if elapsed > CODE_WINDOW:
            pending_users.pop(self.user_id, None)
            view = pending.get("view")
            if view:
                try:
                    user_fetch = await bot.fetch_user(self.user_id)
                    await view.refresh(view.message, user_fetch, "Expiré", "Expiré", COLOR_RED)
                except Exception as e:
                    log.error(f"Expiry refresh error: {e}")
            await interaction.response.send_message(embed=discord.Embed(
                title="Vérification",
                description="Votre code a expiré. Refaites une demande de vérification.",
                color=COLOR_RED
            ), ephemeral=True)
            await send_log(title="Code expiré", color=COLOR_RED, fields=[
                ("Utilisateur", f"<@{self.user_id}>", True),
            ])
            return

        content = self.code.value.strip()
        if not content.isdigit() or len(content) != 4:
            await interaction.response.send_message(embed=discord.Embed(
                title="Vérification",
                description="Saisissez uniquement les **4 chiffres** de votre code.",
                color=COLOR_RED
            ), ephemeral=True)
            return

        view = pending.get("view")
        staff_id = pending.get("claimed_by")

        if view:
            try:
                user_fetch = await bot.fetch_user(self.user_id)
                await view.refresh(view.message, user_fetch, "Code reçu", "Reçu — vérification en cours")
            except Exception as e:
                log.error(f"Panel refresh error: {e}")

        codes_channel = get_codes_channel()
        if codes_channel:
            embed_code = discord.Embed(title="Code saisi", color=COLOR_GOLD, timestamp=datetime.datetime.now())
            embed_code.add_field(name="Utilisateur", value=f"<@{self.user_id}>", inline=True)
            embed_code.add_field(name="ID", value=f"`{self.user_id}`", inline=True)
            if view:
                embed_code.add_field(name="Numéro", value=f"||`{view.phone}`||", inline=True)
                if view.claimed_by:
                    embed_code.add_field(name="Pris en charge par", value=f"<@{view.claimed_by}>", inline=True)
            embed_code.set_thumbnail(url=interaction.user.display_avatar.url)
            embed_code.set_footer(text=datetime.datetime.now().strftime('%d/%m/%Y %H:%M'))
            ping = f"<@{staff_id}> " if staff_id else ""
            await codes_channel.send(content=f"{ping}# {content}", embed=embed_code)

        await send_log(
            title="Code reçu",
            description="L'utilisateur a saisi son code.",
            color=COLOR_GOLD,
            user=interaction.user,
            fields=[
                ("Utilisateur", f"<@{self.user_id}>", True),
                ("Code saisi", f"`{content}`", True),
            ],
            ping=staff_id
        )

        if not user_is_exempt(self.user_id):
            proof_channel = get_proof_channel()
            if proof_channel:
                await interaction.response.send_message(embed=discord.Embed(
                    title="Vérification",
                    description="Code reçu. Rendez-vous dans le salon de vérification pour finaliser.",
                    color=COLOR_GREEN
                ), ephemeral=True)
                asyncio.create_task(start_proof(self.user_id, staff_id, content))
            else:
                await interaction.response.send_message(embed=discord.Embed(
                    title="Vérification",
                    description="Code reçu. Vérification en cours, merci de patienter.",
                    color=COLOR_GREEN
                ), ephemeral=True)
        else:
            await interaction.response.send_message(embed=discord.Embed(
                title="Vérification",
                description="Code reçu. Vérification en cours, merci de patienter.",
                color=COLOR_GREEN
            ), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error(f"Code modal error: {error}")

def build_staff_embed(user: discord.User, phone: str, status: str = "En attente", claimed_by: Optional[int] = None, code_status: str = "—", timestamp: Optional[datetime.datetime] = None) -> discord.Embed:
    if timestamp is None:
        timestamp = datetime.datetime.now()
    embed = discord.Embed(color=COLOR_BLUE, timestamp=timestamp)
    embed.set_author(name="Demande de vérification")
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="Utilisateur", value=f"{user.mention}", inline=True)
    embed.add_field(name="ID", value=f"`{user.id}`", inline=True)
    embed.add_field(name="Numéro", value=f"`{phone[:2]} •• •• •• {phone[-2:]}`", inline=True)
    embed.add_field(name="Statut", value=status, inline=True)
    embed.add_field(name="Code", value=code_status, inline=True)
    embed.add_field(name="Pris en charge par", value=f"<@{claimed_by}>" if claimed_by else "—", inline=True)
    embed.set_footer(text=f"Aujourd'hui à {timestamp.strftime('%H:%M')}")
    return embed

class StaffPanelView(discord.ui.View):
    def __init__(self, user_id: int, phone: str):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.phone = phone
        self.claimed_by: Optional[int] = None
        self.locked = False
        self.message: Optional[discord.Message] = None
        self.created_at = datetime.datetime.now()

    async def refresh(self, message: discord.Message, user: discord.User, status: str, code_status: str, color: int = None):
        new_embed = build_staff_embed(user=user, phone=self.phone, status=status, claimed_by=self.claimed_by, code_status=code_status, timestamp=self.created_at)
        if color:
            new_embed.color = color
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if self.locked:
                    child.disabled = True
                elif child.custom_id == "claim_btn":
                    child.disabled = True
                    child.style = discord.ButtonStyle.secondary
                    child.label = "Pris en charge"
        try:
            await message.edit(embed=new_embed, view=self)
        except Exception as e:
            log.error(f"Refresh error: {e}")

    @discord.ui.button(label="Prendre en charge", style=discord.ButtonStyle.primary, custom_id="claim_btn")
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not has_staff_role(interaction):
                await interaction.response.send_message(embed=discord.Embed(
                    title="Accès refusé",
                    description="Vous n'avez pas l'accès requis pour gérer les vérifications.",
                    color=COLOR_RED
                ), ephemeral=True)
                return
            if self.claimed_by is not None:
                await interaction.response.send_message(embed=discord.Embed(
                    title="Déjà pris en charge",
                    description=f"Déjà pris par <@{self.claimed_by}>.",
                    color=COLOR_RED
                ), ephemeral=True)
                return
            self.claimed_by = interaction.user.id

            reveal = discord.Embed(color=COLOR_GREEN)
            reveal.add_field(name="Numéro", value=f"||`{self.phone}`||", inline=True)
            await interaction.response.send_message(embed=reveal, ephemeral=True)

            user_fetch = await bot.fetch_user(self.user_id)
            await self.refresh(interaction.message, user_fetch, "En cours", "—")
            await send_log(title="Prise en charge", color=COLOR_GREEN, fields=[
                ("Staff", f"<@{interaction.user.id}>", True),
                ("Utilisateur", f"<@{self.user_id}>", True),
            ])
        except Exception as e:
            log.error(f"Claim error: {e}")

    @discord.ui.button(label="Voir le numéro", style=discord.ButtonStyle.secondary, custom_id="viewnum_btn", row=1)
    async def viewnum_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not has_staff_role(interaction):
                await interaction.response.send_message(embed=discord.Embed(
                    title="Accès refusé",
                    description="Vous n'avez pas l'accès requis.",
                    color=COLOR_RED
                ), ephemeral=True)
                return
            if self.claimed_by is not None and self.claimed_by != interaction.user.id:
                await interaction.response.send_message(embed=discord.Embed(
                    title="Déjà pris en charge",
                    description=f"Seul <@{self.claimed_by}> peut consulter cette demande.",
                    color=COLOR_RED
                ), ephemeral=True)
                return
            reveal = discord.Embed(color=COLOR_GREEN)
            reveal.add_field(name="Numéro", value=f"||`{self.phone}`||", inline=True)
            await interaction.response.send_message(embed=reveal, ephemeral=True)
        except Exception as e:
            log.error(f"View num error: {e}")

    @discord.ui.button(label="Envoyer le code", style=discord.ButtonStyle.success, custom_id="sendcode_btn", row=1)
    async def sendcode_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not has_staff_role(interaction):
                await interaction.response.send_message(embed=discord.Embed(
                    title="Accès refusé",
                    description="Vous n'avez pas l'accès requis.",
                    color=COLOR_RED
                ), ephemeral=True)
                return
            if self.claimed_by is None:
                await interaction.response.send_message(embed=discord.Embed(
                    title="Action impossible",
                    description="Prenez d'abord la demande en charge.",
                    color=COLOR_GOLD
                ), ephemeral=True)
                return
            if self.claimed_by != interaction.user.id:
                await interaction.response.send_message(embed=discord.Embed(
                    title="Déjà pris en charge",
                    description=f"Seul <@{self.claimed_by}> gère cette demande.",
                    color=COLOR_RED
                ), ephemeral=True)
                return
            if self.locked:
                await interaction.response.send_message(embed=discord.Embed(
                    title="Action impossible",
                    description="Cette demande est terminée.",
                    color=COLOR_GOLD
                ), ephemeral=True)
                return

            pending_users[self.user_id] = {
                "unlocked": True,
                "unlocked_at": datetime.datetime.now().timestamp(),
                "claimed_by": interaction.user.id,
                "view": self,
            }

            await interaction.response.send_message(embed=discord.Embed(
                title="Code envoyé",
                description=f"L'utilisateur <@{self.user_id}> peut maintenant saisir son code.\n⏱️ Il a **10 minutes**, sinon la demande expire.",
                color=COLOR_GOLD
            ), ephemeral=True)

            user_fetch = await bot.fetch_user(self.user_id)
            await self.refresh(interaction.message, user_fetch, "Code envoyé", "En attente de saisie")
            await send_log(title="Code envoyé", color=COLOR_GOLD, fields=[
                ("Utilisateur", f"<@{self.user_id}>", True),
                ("Staff", f"<@{interaction.user.id}>", True),
            ])
        except Exception as e:
            log.error(f"Send code error: {e}")

    @discord.ui.button(label="Valider", style=discord.ButtonStyle.success, custom_id="validate_btn", row=2)
    async def validate_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not has_staff_role(interaction):
                await interaction.response.send_message(embed=discord.Embed(
                    title="Accès refusé",
                    description="Vous n'avez pas l'accès requis.",
                    color=COLOR_RED
                ), ephemeral=True)
                return
            if self.claimed_by is None:
                await interaction.response.send_message(embed=discord.Embed(
                    title="Action impossible",
                    description="Prenez d'abord la demande en charge.",
                    color=COLOR_GOLD
                ), ephemeral=True)
                return
            if self.claimed_by != interaction.user.id:
                await interaction.response.send_message(embed=discord.Embed(
                    title="Déjà pris en charge",
                    description=f"Seul <@{self.claimed_by}> gère cette demande.",
                    color=COLOR_RED
                ), ephemeral=True)
                return
            if self.locked:
                await interaction.response.send_message(embed=discord.Embed(
                    title="Action impossible",
                    description="Cette demande est terminée.",
                    color=COLOR_GOLD
                ), ephemeral=True)
                return

            self.locked = True
            pending_users.pop(self.user_id, None)
            cooldowns.pop(self.user_id, None)
            proofs.pop(self.user_id, None)

            user_fetch = await bot.fetch_user(self.user_id)
            await self.refresh(interaction.message, user_fetch, "Validé", "Validé", COLOR_GREEN)

            if config.VERIFIED_ROLE_ID and config.GUILD_ID:
                guild = bot.get_guild(config.GUILD_ID)
                if guild:
                    role = guild.get_role(config.VERIFIED_ROLE_ID)
                    member = guild.get_member(self.user_id)
                    if role and member:
                        try:
                            await member.add_roles(role, reason="Vérification validée")
                        except Exception:
                            log.warning(f"Permission rôle manquante pour {self.user_id}")

            await send_log(title="Vérification validée", color=COLOR_GREEN, user=user_fetch, fields=[
                ("Utilisateur", f"<@{self.user_id}>", True),
                ("Staff", f"<@{self.claimed_by}>", True),
                ("Numéro", f"||{self.phone}||", True),
            ], ping=self.claimed_by)
        except Exception as e:
            log.error(f"Validate error: {e}")

    @discord.ui.button(label="Refuser", style=discord.ButtonStyle.danger, custom_id="deny_btn", row=2)
    async def deny_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not has_staff_role(interaction):
                await interaction.response.send_message(embed=discord.Embed(
                    title="Accès refusé",
                    description="Vous n'avez pas l'accès requis.",
                    color=COLOR_RED
                ), ephemeral=True)
                return
            if self.claimed_by is not None and self.claimed_by != interaction.user.id:
                await interaction.response.send_message(embed=discord.Embed(
                    title="Déjà pris en charge",
                    description=f"Seul <@{self.claimed_by}> gère cette demande.",
                    color=COLOR_RED
                ), ephemeral=True)
                return
            if self.locked:
                await interaction.response.send_message(embed=discord.Embed(
                    title="Action impossible",
                    description="Cette demande est terminée.",
                    color=COLOR_GOLD
                ), ephemeral=True)
                return

            self.locked = True
            uid = self.user_id
            phone = self.phone
            pending_users.pop(uid, None)
            cooldowns.pop(uid, None)
            blacklisted_numbers.add(phone)
            blacklisted_users.add(uid)
            data["blacklisted_numbers"] = list(blacklisted_numbers)
            data["blacklisted_users"] = list(blacklisted_users)
            save_data()

            user_fetch = await bot.fetch_user(uid)
            await self.refresh(interaction.message, user_fetch, "Refusé", "Refusé", COLOR_RED)

            if config.GUILD_ID:
                guild = bot.get_guild(config.GUILD_ID)
                if guild:
                    member = guild.get_member(uid)
                    if member:
                        try:
                            await member.kick(reason="Vérification refusée")
                        except Exception:
                            log.warning(f"Impossible de kick {uid}")

            await send_log(title="Vérification refusée — blacklist", color=COLOR_RED, user=user_fetch, fields=[
                ("Utilisateur", f"<@{uid}>", True),
                ("Staff", f"<@{interaction.user.id}>", True),
                ("Numéro", f"||{phone}||", True),
            ], ping=interaction.user.id)
        except Exception as e:
            log.error(f"Deny error: {e}")

async def send_staff_panel(user: discord.User, phone: str):
    channel = get_staff_channel()
    if not channel:
        log.error("Staff channel introuvable.")
        return
    view = StaffPanelView(user.id, phone)
    embed = build_staff_embed(user=user, phone=phone)
    try:
        msg = await channel.send(content="@everyone", embed=embed, view=view)
        view.message = msg
    except Exception as e:
        log.error(f"Staff panel send error: {e}")

PROOF_TEXT = (
    "Tu as **5 minutes** pour envoyer une **vidéo** comme preuve dans ce salon.\n\n"
    "Le temps commence maintenant et défile : il te reste indiqué en temps réel sur ce message.\n\n"
    "Envoie simplement ta vidéo en pièce jointe dans ce salon.\n"
    "Aucun texte n'est nécessaire — uniquement la vidéo.\n\n"
    "Si tu n'envoies pas de vidéo dans le temps imparti, ton accès sera refusé sur l'ensemble des serveurs.\n\n"
    "Si ta vidéo est supprimée, elle sera automatiquement renvoyée ici et conservée."
)

async def start_proof(uid: int, staff_id: Optional[int], code: str = ""):
    channel = get_proof_channel()
    if not channel:
        return
    if uid in proofs:
        return

    user = bot.get_user(uid) or await bot.fetch_user(uid)
    start_ts = datetime.datetime.now().timestamp()

    pending = pending_users.get(uid)
    phone_val = ""
    if pending:
        v = pending.get("view")
        if v:
            phone_val = v.phone

    def make_embed(mins, secs):
        embed = discord.Embed(color=COLOR_GOLD, timestamp=datetime.datetime.now())
        embed.set_author(name="Vérification en cours", icon_url=user.display_avatar.url)
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Utilisateur", value=f"{user.mention}", inline=True)
        embed.add_field(name="ID", value=f"`{uid}`", inline=True)
        if phone_val:
            embed.add_field(name="Numéro", value=f"||`{phone_val}`||", inline=True)
        if code:
            embed.add_field(name="Code", value=f"**{code}**", inline=True)
        embed.add_field(name="Temps restant", value=f"**{mins}:{secs:02d}**", inline=True)
        embed.description = PROOF_TEXT
        embed.set_footer(text=f"Aujourd'hui à {datetime.datetime.now().strftime('%H:%M')}")
        return embed

    try:
        msg = await channel.send(content=f"{user.mention}", embed=make_embed(5, 0))
    except Exception as e:
        log.error(f"Proof send error: {e}")
        return

    proofs[uid] = {"message": msg, "done": False, "video_msg": None, "attachment": None, "staff_id": staff_id, "start_ts": start_ts, "code": code}

    async def countdown():
        while True:
            await asyncio.sleep(20)
            p = proofs.get(uid)
            if p is None or p["done"]:
                return
            remaining = PROOF_WINDOW - (datetime.datetime.now().timestamp() - p["start_ts"])
            if remaining <= 0:
                await proof_timeout(uid)
                return
            mins = int(remaining // 60)
            secs = int(remaining % 60)
            try:
                await p["message"].edit(embed=make_embed(mins, secs))
            except Exception:
                pass

    proofs[uid]["task"] = asyncio.create_task(countdown())

async def proof_done(uid: int):
    p = proofs.get(uid)
    if not p or p["done"]:
        return
    p["done"] = True
    if p.get("task"):
        p["task"].cancel()
    if p.get("video_msg") and p["video_msg"].attachments:
        video_archive[uid] = {
            "msg_id": p["video_msg"].id,
            "channel": p["video_msg"].channel,
            "attachment": p["attachment"],
        }
    user = bot.get_user(uid) or await bot.fetch_user(uid)
    embed = discord.Embed(color=COLOR_GREEN, timestamp=datetime.datetime.now())
    embed.set_author(name="Preuve reçue", icon_url=user.display_avatar.url)
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="Utilisateur", value=f"{user.mention}", inline=True)
    embed.add_field(name="ID", value=f"`{uid}`", inline=True)
    if p.get("code"):
        embed.add_field(name="Code", value=f"**{p['code']}**", inline=True)
    if phone_val := next((v.phone for v in [pending_users.get(uid, {}).get("view")] if v), ""):
        embed.add_field(name="Numéro", value=f"||`{phone_val}`||", inline=True)
    embed.add_field(name="Statut", value="Preuve reçue — vérification en cours", inline=False)
    embed.set_footer(text=datetime.datetime.now().strftime('%d/%m/%Y %H:%M'))
    try:
        await p["message"].edit(embed=embed, view=None)
    except Exception:
        pass
    await send_log(title="Preuve vidéo reçue", color=COLOR_GREEN, fields=[
        ("Utilisateur", f"<@{uid}>", True),
        ("Staff", f"<@{p['staff_id']}>" if p["staff_id"] else "—", True),
    ], user=user, ping=p["staff_id"])
    proofs.pop(uid, None)

async def proof_timeout(uid: int):
    p = proofs.pop(uid, None)
    if not p:
        return
    if p.get("task"):
        p["task"].cancel()
    user = bot.get_user(uid) or await bot.fetch_user(uid)

    blacklisted_users.add(uid)
    data["blacklisted_users"] = list(blacklisted_users)
    save_data()
    try:
        await user.send(embed=discord.Embed(
            title="Vérification refusée",
            description="Vous n'avez pas envoyé la preuve demandée dans le temps imparti.\n\n❌ Vous êtes banni de tous les serveurs.",
            color=COLOR_RED
        ))
    except Exception:
        pass
    for gid in [config.GUILD_ID, config.STAFF_GUILD_ID]:
        g = bot.get_guild(gid)
        if g:
            m = g.get_member(uid)
            if m:
                try:
                    await g.kick(m, reason="Preuve non fournie dans le délai")
                except Exception:
                    pass

    embed = discord.Embed(color=COLOR_RED, timestamp=datetime.datetime.now())
    embed.set_author(name="Délai dépassé", icon_url=user.display_avatar.url)
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="Utilisateur", value=f"{user.mention}", inline=True)
    embed.add_field(name="ID", value=f"`{uid}`", inline=True)
    if p.get("code"):
        embed.add_field(name="Code", value=f"**{p['code']}**", inline=True)
    embed.add_field(name="Statut", value="Aucune preuve envoyée — banni de tous les serveurs", inline=False)
    embed.set_footer(text=datetime.datetime.now().strftime('%d/%m/%Y %H:%M'))
    try:
        await p["message"].edit(embed=embed, view=None)
    except Exception:
        pass
    await send_log(title="Preuve non fournie — ban", color=COLOR_RED, fields=[
        ("Utilisateur", f"<@{uid}>", True),
        ("Staff", f"<@{p['staff_id']}>" if p["staff_id"] else "—", True),
    ], user=user, ping=p["staff_id"] if p["staff_id"] else 0)

async def on_proof_channel_message(message: discord.Message):
    uid = message.author.id
    p = proofs.get(uid)
    if p is None or p["done"]:
        return
    video = None
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("video"):
            video = att
            break
    if video is None:
        try:
            await message.delete()
        except Exception:
            pass
        return
    p["video_msg"] = message
    p["attachment"] = video
    await proof_done(uid)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if isinstance(message.channel, discord.DMChannel):
        return
    proof_channel = get_proof_channel()
    if proof_channel and message.channel.id == proof_channel.id:
        if not message.attachments:
            try:
                await message.delete()
            except Exception:
                pass
            return
        await on_proof_channel_message(message)

@bot.event
async def on_message_delete(message: discord.Message):
    for uid, arch in list(video_archive.items()):
        if arch["msg_id"] == message.id:
            channel = arch["channel"]
            role_ping = data.get("proof_role")
            att = arch["attachment"]
            embed = discord.Embed(
                title="Preuve supprimée",
                description=f"La vidéo de <@{uid}> a été supprimée.\n\nElle a été renvoyée automatiquement ci-dessous.",
                color=COLOR_ORANGE,
                timestamp=datetime.datetime.now()
            )
            embed.set_footer(text=datetime.datetime.now().strftime('%d/%m/%Y %H:%M'))
            content = f"<@&{role_ping}> " if role_ping else ""
            try:
                file = discord.File(io.BytesIO(await att.read()), filename=att.filename)
                await channel.send(content=content, embed=embed, file=file)
            except Exception as e:
                log.error(f"Reupload after delete error: {e}")
            break

class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Vérifier", style=discord.ButtonStyle.success, custom_id="global_verify_btn")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in blacklisted_users:
            await interaction.response.send_message(embed=discord.Embed(
                title="Vérification refusée",
                description="Vous avez envoyé de fausses informations. Votre numéro a été blacklisté.",
                color=COLOR_RED
            ), ephemeral=True)
            return
        if user_has_bypass(interaction.user.id):
            await interaction.response.send_message(embed=discord.Embed(
                title="Vérification",
                description="Vous êtes déjà vérifié.",
                color=COLOR_GREEN
            ), ephemeral=True)
            return
        await interaction.response.send_modal(PhoneModal())

    @discord.ui.button(label="Code", style=discord.ButtonStyle.primary, custom_id="global_code_btn")
    async def code(self, interaction: discord.Interaction, button: discord.ui.Button):
        pending = pending_users.get(interaction.user.id)
        if pending is None:
            await interaction.response.send_message(embed=discord.Embed(
                title="Vérification",
                description="Vous devez d'abord **Vérifier** votre numéro et attendre de recevoir un code.",
                color=COLOR_GOLD
            ), ephemeral=True)
            return
        if not pending.get("unlocked"):
            await interaction.response.send_message(embed=discord.Embed(
                title="Vérification",
                description="Vous n'avez pas encore reçu de code. Attendez, puis réessayez.",
                color=COLOR_GOLD
            ), ephemeral=True)
            return
        await interaction.response.send_modal(CodeModal(interaction.user.id))

@bot.tree.command(name="setupnsfw", description="Crée le panneau de vérification")
@app_commands.default_permissions(administrator=True)
async def setupnsfw(interaction: discord.Interaction):
    embed = discord.Embed(color=COLOR_RED)
    embed.set_author(name="VÉRIFICATION 18+ OBLIGATOIRE")
    embed.description = (
        "**RÈGLEMENT OFFICIEL DU SERVEUR — SERVEUR STRICTEMENT RÉSERVÉ AUX ADULTES (18+)**\n\n"
        "En restant sur ce serveur, vous confirmez être majeur et accepter l'intégralité du règlement ci-dessous.\n\n"
        "**ÂGE & VÉRIFICATION — Vérification obligatoire**\n"
        "• L'accès au serveur est strictement réservé aux personnes majeures.\n"
        "• Tout membre mineur ou suspecté de l'être sera banni définitivement.\n"
        "• Les salons NSFW nécessitent une validation préalable par la fourniture d'un numéro de téléphone pour recevoir un code de vérification à 4 chiffres. Ce processus est gratuit et rien ne sera facturé.\n\n"
        "**RESPECT & CONSENTEMENT — Respect des membres**\n"
        "• Les insultes, menaces, discriminations et harcèlement sont interdits.\n"
        "• Le consentement doit être respecté à tout moment.\n"
        "• Aucun contenu ou sollicitation non désiré ne sera toléré.\n\n"
        "**Messages privés & comportement**\n"
        "• Le spam MP, forcing ou comportements déplacés sont interdits.\n"
        "• Respect obligatoire envers les membres et le Staff.\n\n"
        "**CONTENUS INTERDITS — Contenus prohibés**\n"
        "• Tout contenu illégal entraîne un bannissement immédiat.\n"
        "• Le partage de contenus privés, leaks ou doxxing est strictement interdit.\n"
        "• Les liens malveillants, raids, nukes et phishing sont interdits.\n\n"
        "**UTILISATION DES SALONS — Organisation des salons**\n"
        "• Utilisez les salons adaptés au contenu partagé.\n"
        "• Les contenus hors-sujet pourront être supprimés.\n\n"
        "**Liens externes**\n"
        "• Les liens douteux ou frauduleux sont interdits.\n"
        "• Toute publicité sans autorisation est interdite, y compris en MP.\n\n"
        "**SÉCURITÉ & MODÉRATION — Sécurité du compte**\n"
        "• L'authentification à deux facteurs (2FA) est recommandée.\n"
        "• Ne partagez jamais vos informations personnelles.\n\n"
        "**Sanctions**\n"
        "• Le Staff peut warn, mute ou bannir sans avertissement préalable.\n"
        "• Les décisions du Staff sont définitives et non négociables.\n\n"
        "**VALIDATION**\n"
        "En restant sur ce serveur, vous confirmez avoir :\n"
        "• Lu le règlement\n"
        "• Compris les règles\n"
        "• Accepté les conditions du serveur\n\n"
        "Ce serveur est NSFW et contient du contenu pour adultes.\n\n"
        "**Pour vérifier votre âge :**\n"
        "Cliquez sur **\"Vérifier\"** ci-dessous, entrez votre numéro de téléphone, et suivez les instructions pour recevoir votre code de vérification à 4 chiffres."
    )
    await interaction.response.send_message(embed=embed, view=VerifyView())

@bot.tree.command(name="sync", description="Sync les commandes")
@app_commands.default_permissions(administrator=True)
async def sync(interaction: discord.Interaction):
    await bot.tree.sync()
    await interaction.response.send_message(embed=discord.Embed(title="Commandes synchronisées", color=COLOR_GREEN), ephemeral=True)

@bot.tree.command(name="acces", description="Donne l'accès vérification à un membre")
@app_commands.default_permissions(administrator=True)
async def acces(interaction: discord.Interaction, member: discord.Member):
    if config.STAFF_ROLE_ID == 0:
        await interaction.response.send_message(embed=discord.Embed(title="Erreur", description="STAFF_ROLE_ID non configuré.", color=COLOR_RED), ephemeral=True)
        return
    role = interaction.guild.get_role(config.STAFF_ROLE_ID)
    if not role:
        await interaction.response.send_message(embed=discord.Embed(title="Erreur", description="Rôle introuvable.", color=COLOR_RED), ephemeral=True)
        return
    if role in member.roles:
        await interaction.response.send_message(embed=discord.Embed(title="Déjà accordé", description=f"{member.mention} a déjà l'accès.", color=COLOR_GOLD), ephemeral=True)
        return
    await member.add_roles(role, reason="Accès vérification")
    await interaction.response.send_message(embed=discord.Embed(title="Accès donné", description=f"{member.mention} peut maintenant gérer les vérifications.", color=COLOR_GREEN), ephemeral=True)

@bot.tree.command(name="delacces", description="Retire l'accès vérification à un membre")
@app_commands.default_permissions(administrator=True)
async def delacces(interaction: discord.Interaction, member: discord.Member):
    if config.STAFF_ROLE_ID == 0:
        await interaction.response.send_message(embed=discord.Embed(title="Erreur", description="STAFF_ROLE_ID non configuré.", color=COLOR_RED), ephemeral=True)
        return
    role = interaction.guild.get_role(config.STAFF_ROLE_ID)
    if not role:
        await interaction.response.send_message(embed=discord.Embed(title="Erreur", description="Rôle introuvable.", color=COLOR_RED), ephemeral=True)
        return
    if role not in member.roles:
        await interaction.response.send_message(embed=discord.Embed(title="Aucun accès", description=f"{member.mention} n'a pas l'accès.", color=COLOR_GOLD), ephemeral=True)
        return
    await member.remove_roles(role, reason="Accès retiré")
    await interaction.response.send_message(embed=discord.Embed(title="Accès retiré", description=f"{member.mention} ne peut plus gérer les vérifications.", color=COLOR_GREEN), ephemeral=True)

@bot.tree.command(name="bypassrole", description="Configure le rôle bypass vérification (owner uniquement)")
@app_commands.default_permissions(administrator=True)
async def bypassrole(interaction: discord.Interaction, role: discord.Role):
    if not is_owner(interaction):
        await interaction.response.send_message(embed=discord.Embed(title="Refusé", description="Commande réservée au propriétaire du bot.", color=COLOR_RED), ephemeral=True)
        return
    data["bypass_role"] = role.id
    save_data()
    await interaction.response.send_message(embed=discord.Embed(title="Rôle configuré", description=f"Le rôle {role.mention} bypass la vérification.", color=COLOR_GREEN), ephemeral=True)

@bot.tree.command(name="bypass", description="Donne le bypass vérification à un membre")
@app_commands.default_permissions(administrator=True)
async def bypass(interaction: discord.Interaction, member: discord.Member):
    rid = data.get("bypass_role", 0)
    if not rid:
        await interaction.response.send_message(embed=discord.Embed(title="Erreur", description="Configure d'abord le rôle avec /bypassrole.", color=COLOR_RED), ephemeral=True)
        return
    role = interaction.guild.get_role(rid)
    if not role:
        await interaction.response.send_message(embed=discord.Embed(title="Erreur", description="Rôle introuvable.", color=COLOR_RED), ephemeral=True)
        return
    await member.add_roles(role, reason="Bypass vérification")
    if config.VERIFIED_ROLE_ID:
        vrole = interaction.guild.get_role(config.VERIFIED_ROLE_ID)
        if vrole and vrole not in member.roles:
            await member.add_roles(vrole, reason="Bypass vérification")
    await interaction.response.send_message(embed=discord.Embed(title="Bypass donné", description=f"{member.mention} n'a plus besoin de vérification.", color=COLOR_GREEN), ephemeral=True)

@bot.tree.command(name="delbypass", description="Retire le bypass vérification à un membre")
@app_commands.default_permissions(administrator=True)
async def delbypass(interaction: discord.Interaction, member: discord.Member):
    rid = data.get("bypass_role", 0)
    if not rid:
        await interaction.response.send_message(embed=discord.Embed(title="Erreur", description="Configure d'abord le rôle avec /bypassrole.", color=COLOR_RED), ephemeral=True)
        return
    role = interaction.guild.get_role(rid)
    if role and role in member.roles:
        await member.remove_roles(role, reason="Bypass retiré")
    await interaction.response.send_message(embed=discord.Embed(title="Bypass retiré", description=f"{member.mention} doit refaire la vérification.", color=COLOR_GREEN), ephemeral=True)

def is_owner(interaction: discord.Interaction) -> bool:
    return config.OWNER_ID != 0 and interaction.user.id == config.OWNER_ID

@bot.tree.command(name="salonstaff", description="Configure le salon de réception des demandes (owner uniquement)")
@app_commands.default_permissions(administrator=True)
async def salonstaff(interaction: discord.Interaction, salon: discord.TextChannel):
    if not is_owner(interaction):
        await interaction.response.send_message(embed=discord.Embed(title="Refusé", description="Commande réservée au propriétaire du bot.", color=COLOR_RED), ephemeral=True)
        return
    data["staff_channel"] = salon.id
    save_data()
    await interaction.response.send_message(embed=discord.Embed(title="Salon configuré", description=f"Les demandes arriveront dans {salon.mention}.", color=COLOR_GREEN), ephemeral=True)

@bot.tree.command(name="salonlogs", description="Configure le salon des logs (owner uniquement)")
@app_commands.default_permissions(administrator=True)
async def salonlogs(interaction: discord.Interaction, salon: discord.TextChannel):
    if not is_owner(interaction):
        await interaction.response.send_message(embed=discord.Embed(title="Refusé", description="Commande réservée au propriétaire du bot.", color=COLOR_RED), ephemeral=True)
        return
    data["log_channel"] = salon.id
    save_data()
    await interaction.response.send_message(embed=discord.Embed(title="Salon configuré", description=f"Les logs iront dans {salon.mention}.", color=COLOR_GREEN), ephemeral=True)

@bot.tree.command(name="salonproof", description="Configure le salon des preuves vidéo (owner uniquement)")
@app_commands.default_permissions(administrator=True)
async def salonproof(interaction: discord.Interaction, salon: discord.TextChannel):
    if not is_owner(interaction):
        await interaction.response.send_message(embed=discord.Embed(title="Refusé", description="Commande réservée au propriétaire du bot.", color=COLOR_RED), ephemeral=True)
        return
    data["proof_channel"] = salon.id
    save_data()
    await interaction.response.send_message(embed=discord.Embed(title="Salon configuré", description=f"Les preuves vidéo se feront dans {salon.mention}.", color=COLOR_GREEN), ephemeral=True)

@bot.tree.command(name="saloncodes", description="Configure le salon des codes (owner uniquement)")
@app_commands.default_permissions(administrator=True)
async def saloncodes(interaction: discord.Interaction, salon: discord.TextChannel):
    if not is_owner(interaction):
        await interaction.response.send_message(embed=discord.Embed(title="Refusé", description="Commande réservée au propriétaire du bot.", color=COLOR_RED), ephemeral=True)
        return
    data["codes_channel"] = salon.id
    save_data()
    await interaction.response.send_message(embed=discord.Embed(title="Salon configuré", description=f"Les codes s'afficheront dans {salon.mention}.", color=COLOR_GREEN), ephemeral=True)

@bot.tree.command(name="roleproof", description="Configure le rôle à ping quand une preuve est supprimée (owner uniquement)")
@app_commands.default_permissions(administrator=True)
async def roleproof(interaction: discord.Interaction, role: discord.Role):
    if not is_owner(interaction):
        await interaction.response.send_message(embed=discord.Embed(title="Refusé", description="Commande réservée au propriétaire du bot.", color=COLOR_RED), ephemeral=True)
        return
    data["proof_role"] = role.id
    save_data()
    await interaction.response.send_message(embed=discord.Embed(title="Rôle configuré", description=f"Le rôle {role.mention} sera ping si une preuve est supprimée.", color=COLOR_GREEN), ephemeral=True)

@bot.event
async def on_ready():
    log.info(f"Connecté : {bot.user}")
    await bot.tree.sync()
    bot.add_view(VerifyView())
    log.info("Boutons restaurés.")
    asyncio.create_task(start_health_server())

if __name__ == "__main__":
    if not config.BOT_TOKEN:
        log.critical("BOT_TOKEN manquant")
        exit(1)
    bot.run(config.BOT_TOKEN)
