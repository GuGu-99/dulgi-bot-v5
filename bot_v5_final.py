# -*- coding: utf-8 -*-
# 둘기봇 v6_final_full — Render PostgreSQL 완전통합판
# - 모든 데이터 PostgreSQL DB에 저장 (영구 보존)
# - data.json은 캐시/비상용
# - Render Starter 이상에서 24시간 가동 가능
# - 기존 기능 전부 포함 (보고서/백업/복원/우수사원 등)

import os, io, csv, json, asyncio, datetime, random, aiohttp
import pytz, discord, asyncpg
from discord.ext import commands
from flask import Flask
from threading import Thread

# ========== 기본 설정 ==========
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

# ========== Discord 설정 ==========
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ========== Flask keep-alive ==========
app = Flask(__name__)
@app.route("/")
def home(): return "Bot is alive!"
def run_flask(): app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
def keep_alive(): Thread(target=run_flask, daemon=True).start()

# ========== PostgreSQL 연결 ==========
async def init_db():
    global pool
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"])
    async with pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            data JSONB NOT NULL
        );
        """)

async def load_user(uid):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT data FROM users WHERE id=$1", uid)
        return row["data"] if row else {"attendance": [], "activity": {}, "notified": {}}

async def save_user(uid, data):
    async with pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO users (id, data)
        VALUES ($1, $2)
        ON CONFLICT (id) DO UPDATE SET data=$2;
        """, uid, json.dumps(data))

# ========== 날짜 관련 ==========
def logical_date_str():
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

# ========== 점수 반영 ==========
async def add_points(uid, channel_id, conf):
    user = await load_user(uid)
    date_str = logical_date_str()
    today_rec = user["activity"].setdefault(date_str, {"total": 0, "by_channel": {}})
    prev = today_rec["by_channel"].get(str(channel_id), 0)
    if prev + conf["points"] > conf["daily_max"]:
        return False
    today_rec["by_channel"][str(channel_id)] = prev + conf["points"]
    today_rec["total"] += conf["points"]
    await save_user(uid, user)
    return True

# ========== 시각화 ==========
def get_week_progress(data, uid, ref_date, daily_goal=10):
    start, _ = get_week_range(ref_date)
    labels = ["월", "화", "수", "목", "금", "토", "일"]
    blocks, cur = [], start
    for _ in range(7):
        ds = cur.strftime("%Y-%m-%d")
        pts = data["activity"].get(ds, {}).get("total", 0)
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
        pts = data["activity"].get(ds, {}).get("total", 0)
        cells.append("🟩" if pts >= daily_goal else "⬜")
    rows = [" ".join(cells[r*5:(r+1)*5]) for r in range(4)]
    return "월간 활동 (1~20일 기준)\n" + "\n".join(rows)

# ========== 축하 DM ==========
async def check_milestones(user, uid):
    data = await load_user(uid)
    today = datetime.datetime.now(KST).date()
    start, end = get_week_range(today)
    weekly_total = sum(
        rec.get("total", 0)
        for ds, rec in data["activity"].items()
        if start <= datetime.datetime.strptime(ds, "%Y-%m-%d").date() <= end
    )
    prefix = f"{today.year}-{today.month:02d}"
    monthly_total = sum(
        rec.get("total", 0)
        for ds, rec in data["activity"].items()
        if ds.startswith(prefix)
    )
    notified = data.setdefault("notified", {})
    wkey = week_key(today)
    mkey = f"{today.year}-{today.month:02d}"
    if weekly_total >= WEEKLY_BEST_THRESHOLD and not notified.get(f"weekly_{wkey}"):
        msg = random.choice([
            f"🌿 이번 주 {weekly_total}점 돌파! 당신의 꾸준한 열정이 정말 멋져요. 다음 주도 함께 성장해봐요 💪",
            f"🌸 한 주 동안 꾸준히 쌓아온 {weekly_total}점, 정말 대단해요! 다음 주도 파이팅이에요 ☀️",
            f"☕ 이번 주 목표 달성! 작은 노력들이 이렇게 멋진 결과를 만들었어요. 다음 주도 함께 달려봐요 🌈"
        ])
        await user.send(msg)
        notified[f"weekly_{wkey}"] = True
    if monthly_total >= MONTHLY_BEST_THRESHOLD and not notified.get(f"monthly_{mkey}"):
        msg = random.choice([
            f"🏆 {today.month}월 {monthly_total}점 달성! 한 달간의 꾸준한 노력, 정말 자랑스러워요. 다음 달에도 함께 멋지게 나아가요 ✨",
            f"🌟 한 달 동안 쌓아온 {monthly_total}점, 그 열정과 성실함이 정말 대단해요. 다음 달에도 멋진 기록을 함께 만들어봐요 💪",
            f"💫 {today.month}월 목표 달성! 노력의 결실이 반짝이고 있어요. 다음 달에도 천천히, 꾸준히 함께 가요 🌿"
        ])
        await user.send(msg)
        notified[f"monthly_{mkey}"] = True
    await save_user(uid, data)

# ========== 봇 이벤트 ==========
@bot.event
async def on_ready():
    print(f"✅ 로그인 완료: {bot.user}")
    keep_alive()
    await init_db()
    bot.loop.create_task(schedule_backup_loop())

# ========== 메시지 감지 ==========
@bot.event
async def on_message(msg):
    if msg.author.bot: return
    cid = msg.channel.id
    conf = CHANNEL_POINTS.get(cid)
    if not conf:
        await bot.process_commands(msg); return
    uid = str(msg.author.id)
    countable = True
    if conf["image_only"]:
        countable = any(a.content_type and a.content_type.startswith("image/") for a in msg.attachments)
    if not countable:
        await bot.process_commands(msg); return
    if await add_points(uid, cid, conf):
        await check_milestones(msg.author, uid)
    await bot.process_commands(msg)

# ========== 출근 ==========
@bot.command(name="출근")
async def cmd_checkin(ctx):
    uid = str(ctx.author.id)
    user = await load_user(uid)
    today = logical_date_str()
    if today in user["attendance"]:
        return await ctx.reply("이미 출근 완료 🕐")
    user["attendance"].append(today)
    await save_user(uid, user)
    await add_points(uid, 1423359791287242782, CHANNEL_POINTS[1423359791287242782])
    await ctx.reply("✅ 출근 완료! (+4점) 오늘도 힘내요!")

# ========== 보고서 ==========
@bot.command(name="보고서")
async def cmd_report(ctx):
    uid = str(ctx.author.id)
    data = await load_user(uid)
    today = datetime.datetime.now(KST).date()
    msg = (f"🌼 {ctx.author.display_name}님의 이번 주 활동 요약\n\n"
           f"🕐 출근 횟수: {len(data['attendance'])}회\n"
           f"💬 총 점수: {sum(rec.get('total',0) for rec in data['activity'].values())}점\n\n"
           f"📊 주간 활동:\n{get_week_progress(data, uid, today)}\n\n"
           f"{get_month_grid_5x4(data, uid, today)}")
    await ctx.author.send(msg)

# ========== 백업 ==========
def local_backup():
    async def _():
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM users")
            data = {r["id"]: r["data"] for r in rows}
            with open(BACKUP_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
    return asyncio.create_task(_())

async def schedule_backup_loop():
    while True:
        now = datetime.datetime.now(KST)
        next6 = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if next6 < now: next6 += datetime.timedelta(days=1)
        await asyncio.sleep((next6 - now).total_seconds())
        await local_backup()
        print("✅ Daily backup at 06:00 KST")

@bot.command(name="백업")
async def cmd_backup(ctx):
    await local_backup()
    await ctx.reply("✅ 데이터베이스 백업 완료!")

# ========== 복원 ==========
@bot.command(name="PP복원")
async def cmd_restore(ctx, link=None):
    if not link: return await ctx.reply("⚠️ 사용법: !PP복원 [JSON 링크]")
    async with aiohttp.ClientSession() as s:
        async with s.get(link) as r:
            text = await r.text()
    try:
        j = json.loads(text)
        async with pool.acquire() as conn:
            for uid, data in j.items():
                await conn.execute(
                    "INSERT INTO users (id,data) VALUES($1,$2) ON CONFLICT(id) DO UPDATE SET data=$2;",
                    uid, json.dumps(data)
                )
        await ctx.reply("✅ 복원 완료!")
    except Exception as e:
        await ctx.reply(f"⚠️ 오류: {e}")

# ========== 관리자 리포트 ==========
def is_admin(m): return getattr(m.guild_permissions, "manage_guild", False)

@bot.command(name="PP보고서")
async def cmd_admin_report(ctx, 기간=None, *args):
    if not is_admin(ctx.author):
        return await ctx.reply("⚠️ 관리자만 사용 가능합니다.")
    today = datetime.datetime.now(KST).date()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM users")
        data = {r["id"]: r["data"] for r in rows}
    if 기간 == "주간":
        start, end = get_week_range(today)
        totals = []
        for uid, user in data.items():
            t = sum(rec.get("total", 0) for ds, rec in user["activity"].items()
                    if start <= datetime.datetime.strptime(ds, "%Y-%m-%d").date() <= end)
            totals.append((uid, t))
        totals.sort(key=lambda x: x[1], reverse=True)
        csv_buf = io.StringIO()
        w = csv.writer(csv_buf); w.writerow(["이름","ID","주간점수"])
        for uid, t in totals:
            try: name = (await ctx.guild.fetch_member(int(uid))).display_name
            except: name = uid
            w.writerow([name, uid, t])
        await ctx.reply("📊 주간 보고서",
            file=discord.File(io.BytesIO(csv_buf.getvalue().encode()), f"weekly_{today}.csv"))
    elif 기간 == "월간":
        m = today.month
        if args: m = int(args[0].replace("월", ""))
        totals = []
        for uid, user in data.items():
            t = sum(rec.get("total", 0) for ds, rec in user["activity"].items()
                    if ds.startswith(f"{today.year}-{m:02d}"))
            totals.append((uid, t))
        totals.sort(key=lambda x: x[1], reverse=True)
        csv_buf = io.StringIO()
        w = csv.writer(csv_buf); w.writerow(["이름","ID","월간점수"])
        for uid, t in totals:
            try: name = (await ctx.guild.fetch_member(int(uid))).display_name
            except: name = uid
            w.writerow([name, uid, t])
        await ctx.reply("📅 월간 보고서",
            file=discord.File(io.BytesIO(csv_buf.getvalue().encode()), f"monthly_{m}월.csv"))
    else:
        await ctx.reply("⚠️ 사용법: !PP보고서 주간 / !PP보고서 월간 N월")

# ========== 시작 ==========
if __name__ == "__main__":
    TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
    asyncio.run(bot.start(TOKEN))
