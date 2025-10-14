# -*- coding: utf-8 -*-
# 둘기봇 v5.3.2_final — Render 무료플랜 대응 완전판
# ✅ 주요 기능:
# - 출근 시 자동 +4점 반영
# - 이미지/링크 점수 자동 인식
# - !보고서 : 주간 점수 + 잔디 시각화
# - !PP보고서 주간 / 월간 N월 : 관리자 전용 CSV
# - !PP복원 [링크] : Discord 백업파일 복구
# - 자동 백업 (매일 오전 6시)
# - Flask keep-alive (Render 호환)
# - 주간/월간 우수사원 달성 시 DM 축하 알림

import os
import io
import csv
import json
import random
import asyncio
import datetime
import pytz
import aiohttp
from typing import Dict
from flask import Flask
from threading import Thread
import discord
from discord.ext import commands

# ========= 기본 설정 =========
KST = pytz.timezone("Asia/Seoul")
DATA_FILE = "data.json"
BACKUP_FILE = "data_backup.json"

CHANNEL_POINTS = {
    1423170386811682908: {"name": "일일-그림보고", "points": 6, "daily_max": 6, "image_only": True},
    1423172691724079145: {"name": "자유채팅판", "points": 1, "daily_max": 4, "image_only": False},
    1423359059566006272: {"name": "정보-공모전", "points": 1, "daily_max": 1, "image_only": False},
    1423170949477568623: {"name": "정보-그림꿀팁", "points": 1, "daily_max": 1, "image_only": False},
    1423242322665148531: {"name": "고민상담", "points": 1, "daily_max": 1, "image_only": False},
    1423359791287242782: {"name": "출퇴근기록", "points": 4, "daily_max": 4, "image_only": False},
    1423171509752434790: {"name": "다-그렸어요", "points": 5, "daily_max": 5, "image_only": True},
}

WEEKLY_BEST_THRESHOLD = 60
MONTHLY_BEST_THRESHOLD = 200

# ========= 데이터 유틸 =========
def load_data(path=DATA_FILE):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_data(data, path=DATA_FILE):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def ensure_user(data, uid):
    if "users" not in data:
        data["users"] = {}
    if uid not in data["users"]:
        data["users"][uid] = {"attendance": [], "activity": {}, "notified": {}}

def logical_date_str_from_now():
    now = datetime.datetime.now(KST)
    logical = now - datetime.timedelta(hours=6)
    return logical.strftime("%Y-%m-%d")

def get_week_range(d):
    start = d - datetime.timedelta(days=d.weekday())
    end = start + datetime.timedelta(days=6)
    return start, end

def week_key(d):
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"

def add_activity_logic(data, uid, date_str, channel_id, channel_points_map):
    ensure_user(data, uid)
    conf = channel_points_map.get(channel_id)
    if not conf:
        return False, []
    points, ch_max = conf["points"], conf["daily_max"]

    user = data["users"][uid]
    if date_str not in user["activity"]:
        user["activity"][date_str] = {"total": 0, "by_channel": {}}
    today_rec = user["activity"][date_str]
    ckey = str(channel_id)
    prev = today_rec["by_channel"].get(ckey, 0)

    if prev + points > ch_max:
        return False, []

    today_rec["by_channel"][ckey] = prev + points
    today_rec["total"] += points
    return True, []

# ========= 시각화 =========
def get_week_progress(data, uid, ref_date, daily_goal=10):
    start, _ = get_week_range(ref_date)
    labels = ["월", "화", "수", "목", "금", "토", "일"]
    blocks = []
    cur = start
    for _ in range(7):
        ds = cur.strftime("%Y-%m-%d")
        pts = data["users"][uid]["activity"].get(ds, {}).get("total", 0)
        blocks.append("🟩" if pts >= daily_goal else "⬜")
        cur += datetime.timedelta(days=1)
    return " ".join(labels) + "\n" + " ".join(blocks)

def get_month_grid_5x4(data, uid, ref_date, daily_goal=10):
    first = ref_date.replace(day=1)
    next_month = (first.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    month_days = (next_month - datetime.timedelta(days=1)).day
    cells = []
    for day in range(1, 21):
        if day > month_days:
            cells.append("  ")
            continue
        ds = first.replace(day=day).strftime("%Y-%m-%d")
        pts = data["users"][uid]["activity"].get(ds, {}).get("total", 0)
        cells.append("🟩" if pts >= daily_goal else "⬜")
    rows = [" ".join(cells[r*5:(r+1)*5]) for r in range(4)]
    return "월간 활동 (1~20일 기준)\n" + "\n".join(rows)

# ========= Discord & Flask =========
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
data_store = load_data()

app = Flask(__name__)
@app.route("/")
def home(): return "Bot is alive!"
def run_flask(): app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
def keep_alive(): Thread(target=run_flask, daemon=True).start()

# ========= 이벤트 =========
@bot.event
async def on_ready():
    print(f"✅ 로그인 완료: {bot.user}")
    keep_alive()
    bot.backup_task = asyncio.create_task(schedule_daily_backup_loop())

# ========= 출근 =========
@bot.command(name="출근")
async def check_in(ctx):
    uid = str(ctx.author.id)
    today = logical_date_str_from_now()
    ensure_user(data_store, uid)
    if today in data_store["users"][uid]["attendance"]:
        return await ctx.reply("이미 출근 완료 🕐")
    data_store["users"][uid]["attendance"].append(today)
    add_activity_logic(data_store, uid, today, 1423359791287242782, CHANNEL_POINTS)
    save_data(data_store)
    await ctx.reply("✅ 출근 완료! (+4점) 오늘도 힘내요!")

# ========= 메시지 감지 =========
@bot.event
async def on_message(msg):
    if msg.author.bot: return
    cid = msg.channel.id
    uid = str(msg.author.id)
    today = logical_date_str_from_now()
    ensure_user(data_store, uid)
    conf = CHANNEL_POINTS.get(cid)
    if not conf:
        await bot.process_commands(msg); return
    countable = True
    if cid == 1423171509752434790:
        has_link = "http" in msg.content
        has_attach = len(msg.attachments) > 0
        countable = has_link or has_attach
    elif conf["image_only"]:
        countable = any(a.content_type and a.content_type.startswith("image/") for a in msg.attachments)
    if not countable:
        await bot.process_commands(msg); return
    add_activity_logic(data_store, uid, today, cid, CHANNEL_POINTS)
    save_data(data_store)
    await check_milestones(msg.author, uid)
    await bot.process_commands(msg)

# ========= 축하 알림 (주간 / 월간 달성 시 DM 발송) =========
async def check_milestones(user, uid: str):
    today = datetime.datetime.now(KST).date()
    ensure_user(data_store, uid)

    # --- 주간 점수 ---
    start, end = get_week_range(today)
    weekly_total = sum(
        rec.get("total", 0)
        for ds, rec in data_store["users"][uid]["activity"].items()
        if start <= datetime.datetime.strptime(ds, "%Y-%m-%d").date() <= end
    )

    # --- 월간 점수 ---
    prefix = f"{today.year}-{today.month:02d}"
    monthly_total = sum(
        rec.get("total", 0)
        for ds, rec in data_store["users"][uid]["activity"].items()
        if ds.startswith(prefix)
    )

    notified = data_store["users"][uid].setdefault("notified", {})
    wkey = week_key(today)
    mkey = f"{today.year}-{today.month:02d}"

    # ✅ 주간 우수사원 (60점 이상)
    if weekly_total >= WEEKLY_BEST_THRESHOLD and not notified.get(f"weekly_{wkey}"):
        try:
            msg = random.choice([
                f"🌿 이번 주 {weekly_total}점 돌파! 당신의 꾸준한 열정이 정말 멋져요. 다음 주도 함께 성장해봐요 💪",
                f"🌸 한 주 동안 꾸준히 쌓아온 {weekly_total}점, 정말 대단해요! 다음 주도 파이팅이에요 ☀️",
                f"☕ 이번 주 목표 달성! 작은 노력들이 이렇게 멋진 결과를 만들었어요. 다음 주도 함께 달려봐요 🌈"
            ])
            await user.send(msg)
            notified[f"weekly_{wkey}"] = True
        except: pass

    # ✅ 월간 우수사원 (200점 이상)
    if monthly_total >= MONTHLY_BEST_THRESHOLD and not notified.get(f"monthly_{mkey}"):
        try:
            msg = random.choice([
                f"🏆 {today.month}월 {monthly_total}점 달성! 한 달간의 꾸준한 노력, 정말 자랑스러워요. 다음 달에도 함께 멋지게 나아가요 ✨",
                f"🌟 한 달 동안 쌓아온 {monthly_total}점, 그 열정과 성실함이 정말 대단해요. 다음 달에도 멋진 기록을 함께 만들어봐요 💪",
                f"💫 {today.month}월 목표 달성! 노력의 결실이 반짝이고 있어요. 다음 달에도 천천히, 꾸준히 함께 가요 🌿"
            ])
            await user.send(msg)
            notified[f"monthly_{mkey}"] = True
        except: pass

    save_data(data_store)

# ========= 보고서 =========
@bot.command(name="보고서")
async def report(ctx):
    uid = str(ctx.author.id)
    today = datetime.datetime.now(KST).date()
    ensure_user(data_store, uid)
    att = len(data_store["users"][uid]["attendance"])
    total = sum(rec.get("total", 0) for rec in data_store["users"][uid]["activity"].values())
    msg = (f"🌼 {ctx.author.display_name}님의 이번 주 활동 요약\n\n"
           f"🕐 출근 횟수: {att}회\n"
           f"💬 총 점수: {total}점\n\n"
           f"📊 주간 활동:\n{get_week_progress(data_store, uid, today)}\n\n"
           f"{get_month_grid_5x4(data_store, uid, today)}")
    await ctx.author.send(msg)

# ========= 백업/복원 =========
def backup_now():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = f.read()
        with open(BACKUP_FILE, "w", encoding="utf-8") as f:
            f.write(data)
        return True
    return False

async def schedule_daily_backup_loop():
    while True:
        now = datetime.datetime.now(KST)
        next_backup = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if next_backup < now:
            next_backup += datetime.timedelta(days=1)
        await asyncio.sleep((next_backup - now).total_seconds())
        backup_now()
        print("✅ Daily backup at 06:00 KST")

def is_admin(m): return getattr(m.guild_permissions, "manage_guild", False)

@bot.command(name="백업")
async def cmd_backup(ctx):
    if not is_admin(ctx.author):
        return await ctx.reply("관리자만 가능해요.")
    ok = backup_now()
    await ctx.reply("✅ 백업 완료!" if ok else "⚠️ 백업 실패")

# ========= 외부 복원 =========
@bot.command(name="PP복원")
async def cmd_restore_from_link(ctx, file_url: str = None):
    if not is_admin(ctx.author):
        return await ctx.reply("관리자만 가능해요.")
    if not file_url:
        return await ctx.reply("사용법: `!PP복원 [백업파일 링크]`")
    if not (file_url.startswith("https://cdn.discordapp.com/") or file_url.startswith("https://media.discordapp.net/")):
        return await ctx.reply("⚠️ Discord 업로드 링크만 허용돼요!")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(file_url) as r:
                if r.status != 200:
                    return await ctx.reply("⚠️ 파일 불러오기 실패")
                text = await r.text()
        data_json = json.loads(text)
        if "users" not in data_json:
            return await ctx.reply("⚠️ 잘못된 JSON 구조")
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data_json, f, ensure_ascii=False, indent=4)
        global data_store
        data_store = data_json
        await ctx.reply("✅ 복원 완료! data.json 갱신됨")
    except Exception as e:
        await ctx.reply(f"⚠️ 복원 중 오류: {e}")

# ========= 관리자 보고서 =========
def all_users_week_total(data, ref_date):
    start, end = get_week_range(ref_date)
    ret = []
    for uid in data.get("users", {}):
        total = sum(rec.get("total", 0) for ds, rec in data["users"][uid]["activity"].items())
        ret.append((uid, total))
    return sorted(ret, key=lambda x: x[1], reverse=True)

@bot.command(name="PP보고서")
async def cmd_pp_report(ctx, 기간: str = None, *args):
    if not is_admin(ctx.author):
        return await ctx.reply("관리자만 가능해요.")
    if 기간 not in ("주간", "월간"):
        return await ctx.reply("사용법: `!PP보고서 주간` 또는 `!PP보고서 월간 N월`")
    today = datetime.datetime.now(KST).date()
    if 기간 == "주간":
        pairs = all_users_week_total(data_store, today)
        csv_buf = io.StringIO()
        w = csv.writer(csv_buf)
        w.writerow(["사용자명", "ID", "주간점수"])
        for uid, sc in pairs:
            try:
                m = await ctx.guild.fetch_member(int(uid))
                name = m.display_name
            except:
                name = uid
            w.writerow([name, uid, sc])
        await ctx.reply("📊 주간 보고서", file=discord.File(io.BytesIO(csv_buf.getvalue().encode("utf-8")), "weekly.csv"))
    if 기간 == "월간":
        m = today.month
        if args: m = int(args[0].replace("월", ""))
        pairs = all_users_week_total(data_store, today.replace(month=m))
        csv_buf = io.StringIO()
        w = csv.writer(csv_buf)
        w.writerow(["사용자명", "ID", "월간점수"])
        for uid, sc in pairs:
            try:
                m = await ctx.guild.fetch_member(int(uid))
                name = m.display_name
            except:
                name = uid
            w.writerow([name, uid, sc])
        await ctx.reply("📅 월간 보고서", file=discord.File(io.BytesIO(csv_buf.getvalue().encode("utf-8")), f"monthly_{m}월.csv"))

# ========= 시작 =========
if __name__ == "__main__":
    TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("❌ DISCORD_BOT_TOKEN 환경변수가 설정되지 않았습니다.")
