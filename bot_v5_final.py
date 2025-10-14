# test_bot.py
import os, discord, asyncio
from discord.ext import commands

TOKEN = os.environ.get("DISCORD_BOT_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ 로그인 완료: {bot.user}")

@bot.command(name="테스트")
async def test_cmd(ctx):
    await ctx.reply("✅ 봇 명령어가 정상적으로 작동 중입니다!")

if __name__ == "__main__":
    bot.run(TOKEN)
