"""Commande /annonce pour publier des embeds personnalisés."""

from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands


COLOURS = {
    "red": 0xED4245,
    "yellow": 0xFEE75C,
    "green": 0x57F287,
    "blue": 0x5865F2,
    "purple": 0x9B59B6,
    "orange": 0xE67E22,
}


def valid_url(value: str | None) -> bool:
    if not value:
        return True
    value = value.strip()
    if not value:
        return True
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


class Announcement(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="annonce", description="Publie une annonce sous forme d'embed")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        titre="Titre de l'annonce",
        description="Contenu de l'annonce",
        couleur="Couleur de l'embed",
        image_url="URL de l'image principale (facultatif)",
        thumbnail_url="URL de la petite image (facultative)",
    )
    @app_commands.choices(
        couleur=[
            app_commands.Choice(name="Rouge", value="red"),
            app_commands.Choice(name="Jaune", value="yellow"),
            app_commands.Choice(name="Vert", value="green"),
            app_commands.Choice(name="Bleu", value="blue"),
            app_commands.Choice(name="Violet", value="purple"),
            app_commands.Choice(name="Orange", value="orange"),
        ]
    )
    async def annonce(
        self,
        interaction: discord.Interaction,
        titre: str,
        description: str,
        couleur: app_commands.Choice[str],
        image_url: str | None = None,
        thumbnail_url: str | None = None,
    ):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Tu dois être administrateur pour utiliser cette commande.",
                ephemeral=True,
            )
            return

        image_url = image_url.strip() if image_url else None
        thumbnail_url = thumbnail_url.strip() if thumbnail_url else None

        if (image_url is not None and not valid_url(image_url)) or (
            thumbnail_url is not None and not valid_url(thumbnail_url)
        ):
            await interaction.response.send_message(
                "Les URLs doivent commencer par http:// ou https://.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=titre[:256],
            description=description[:4096],
            colour=COLOURS.get(couleur.value, COLOURS["blue"]),
            timestamp=discord.utils.utcnow(),
        )

        if image_url:
            embed.set_image(url=image_url)
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)

        embed.set_footer(text="Annonce publiée par le bot")

        await interaction.response.send_message("Annonce publiée.", ephemeral=True)
        await interaction.channel.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Announcement(bot))
