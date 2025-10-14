# -*- coding: utf-8 -*-
# 둘기봇 v5 (단독 실행 완성버전)
# ---------------------------------------
# - PostgreSQL 영구 저장 (asyncpg)
# - 오전 6시 기준 날짜 계산
# - 매일 06시 자동 백업 (Discord 채널 업로드)
# - 개인 보고서(이미지 카드)
# - 관리자 리포트 + CSV
# - Flask keep-alive (Render 호환)
# ---------------------------------------

import os, io, csv, json, random, asyncio, datetime, pytz
from typing import Dict, Tuple, List
from flask import Flask
from threading import Thread

import discord
from discord.ext import commands
import asyncpg
from PIL import Image, ImageDraw, ImageFont

# ========= 기본 설정 =========
KST = pytz.timezone("Asia/Seoul")
LOGICAL_DAY_START_HOUR = 6  # 06시부터 하루 시작
WEEKLY_BEST_THRESHOLD = 60

DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DB_URL = os.environ.get("DATABASE_URL")
try:
    BACKUP_CHANNEL_ID = int(os.environ.get("BACKUP_CHANNEL_ID", "0"))
except:
    BACKUP_CHANNEL_ID = 0

DB_POOL = None

# ========= 채널 점수체계 =========
CHANNEL_POINTS = {
    1423170386811682908: {"name": "일일-그림보고", "points": 6, "daily_max": 6, "image_only": True},
    1423172691724079145: {"name": "자유채팅판", "points": 1, "daily_max": 4, "image_only": False},
    1423359059566006272: {"name": "정보-공모전", "points": 1, "daily_max": 1, "image_only": False},
    1423170949477568623: {"name": "정보-그림꿀팁", "points": 1, "daily_max": 1, "image_only": False},
    1423242322665148531: {"name": "고민상담", "points": 1, "daily_max": 1, "image_only": False},
    1423359791287242782: {"name": "출퇴근기록", "points": 4, "daily_max": 4, "image_only": False},
    1423171509752434790: {"name": "다-그렸어요", "points": 5, "daily_max": 5, "image_only": True},
}

# ========= 시간 관련 =========
def now_kst(): return datetime.datetime.now(KST)
def logical_date_from_dt(dt):
    if dt.hour < LOGICAL_DAY_START_HOUR: dt -= datetime.timedelta(days=1)
    return dt.date()
def logical_date_str(dt): return logical_date_from_dt(dt).strftime("%Y-%m-%d")
def get_week_range(d):
    start = d - datetime.timedelta(days=d.weekday())
    return start, start + datetime.timedelta(days=6)
def week_key(d):
    y, w, _ = d.isocalendar(); return f"{y}-W{w:02d}"

# ========= Flask keep-alive =========
app = Flask(__name__)
@app.route("/")
def home(): return "둘기봇 is alive!"
def run_flask(): app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
def keep_alive():
    t = Thread(target=run_flask, daemon=True); t.start()

# ========= Discord 설정 =========
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ========= DB 초기화 =========
async def init_db():
    global DB_POOL
    DB_POOL = await asyncpg.create_pool(dsn=DB_URL)
    async with DB_POOL.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users(uid TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS attendance(uid TEXT REFERENCES users(uid), date TEXT, PRIMARY KEY(uid,date));
        CREATE TABLE IF NOT EXISTS activity(uid TEXT REFERENCES users(uid), date TEXT, total INT, by_channel JSONB, PRIMARY KEY(uid,date));
        CREATE TABLE IF NOT EXISTS notified(uid TEXT REFERENCES users(uid), week_key TEXT, milestones INT[], PRIMARY KEY(uid,week_key));
        """)

async def ensure_user(uid:str):
    async with DB_POOL.acquire() as c:
        await c.execute("INSERT INTO users(uid) VALUES($1) ON CONFLICT DO NOTHING", uid)

# ========= 데이터 연산 =========
async def db_get_day(uid, date):
    async with DB_POOL.acquire() as c:
        row = await c.fetchrow("SELECT total,by_channel FROM activity WHERE uid=$1 AND date=$2", uid, date)
        if not row: return 0, {}
        return int(row["total"] or 0), dict(row["by_channel"] or {})

async def db_set_day(uid, date, total, by_channel):
    async with DB_POOL.acquire() as c:
        await c.execute("""INSERT INTO activity(uid,date,total,by_channel)
        VALUES($1,$2,$3,$4)
        ON CONFLICT(uid,date) DO UPDATE SET total=$3,by_channel=$4""",
        uid, date, total, json.dumps(by_channel, ensure_ascii=False))

async def db_get_week_total(uid, ref_date):
    start, end = get_week_range(ref_date)
    async with DB_POOL.acquire() as c:
        r = await c.fetchrow("SELECT SUM(total) AS s FROM activity WHERE uid=$1 AND date>=$2 AND date<=$3",
            uid, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
        return int(r["s"]) if r["s"] else 0

async def db_get_notified(uid, wk):
    async with DB_POOL.acquire() as c:
        r = await c.fetchrow("SELECT milestones FROM notified WHERE uid=$1 AND week_key=$2", uid, wk)
        if not r or not r["milestones"]: return set()
        return set(r["milestones"])
async def db_set_notified(uid, wk, m):
    async with DB_POOL.acquire() as c:
        await c.execute("""INSERT INTO notified(uid,week_key,milestones)
        VALUES($1,$2,$3)
        ON CONFLICT(uid,week_key) DO UPDATE SET milestones=$3""", uid, wk, list(sorted(m)))

# ========= 점수 추가 =========
async def add_activity(uid, date, cid, cap=None):
    await ensure_user(uid)
    conf = CHANNEL_POINTS.get(cid)
    if not conf: return False, []
    pts = conf["points"]; ch_max = conf["daily_max"]; ck = str(cid)
    total, by_ch = await db_get_day(uid, date)
    prev = by_ch.get(ck, 0)
    if prev + pts > ch_max: return False, []
    if cap and total + pts > cap: return False, []

    by_ch[ck] = prev + pts; total += pts
    await db_set_day(uid, date, total, by_ch)

    ref = datetime.datetime.strptime(date, "%Y-%m-%d").date()
    wk = week_key(ref)
    prev_week = await db_get_week_total(uid, ref)
    notif = await db_get_notified(uid, wk)
    new_total = await db_get_week_total(uid, ref)
    prev_lv, new_lv = prev_week//50, new_total//50
    new = []
    if new_lv > prev_lv:
        for lv in range(prev_lv+1, new_lv+1):
            ms = lv*50
            if ms not in notif: new.append(ms); notif.add(ms)
        await db_set_notified(uid, wk, notif)
    return True, new

# ========= 보고서 이미지 =========
def render_card(name, att, pts, grid):
    img = Image.new("RGB",(560,240),(245,245,250))
    d=ImageDraw.Draw(img)
    try:f=ImageFont.truetype("arial.ttf",18)
    except:f=ImageFont.load_default()
    d.text((20,20),f"{name}님의 주간 리포트",fill=(30,30,50),font=f)
    d.text((20,50),f"출근 {att}회 | 점수 {pts}점",fill=(60,60,80),font=f)
    y=90
    for line in grid.split("\n"):
        d.text((20,y),line,fill=(40,40,60),font=f);y+=25
    buf=io.BytesIO();img.save(buf,"PNG");buf.seek(0)
    return buf

async def week_grid(uid, ref, goal=10):
    start,_=get_week_range(ref)
    labels="월 화 수 목 금 토 일".split()
    out=[[],[]]
    async with DB_POOL.acquire() as c:
        for i in range(7):
            ds=(start+datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            r=await c.fetchrow("SELECT total FROM activity WHERE uid=$1 AND date=$2",uid,ds)
            t=int(r["total"]) if r and r["total"] else 0
            out[0].append(labels[i]); out[1].append("🟩" if t>=goal else "⬜")
    return " ".join(out[0])+"\n"+" ".join(out[1])

# ========= Discord 이벤트 =========
@bot.event
async def on_ready():
    print(f"✅ 로그인 완료: {bot.user}")
    keep_alive()
    await init_db()
    bot.backup_task = asyncio.create_task(daily_backup_loop())

@bot.command(name="출근")
async def check_in(ctx):
    uid=str(ctx.author.id); await ensure_user(uid)
    ds=logical_date_str(now_kst())
    async with DB_POOL.acquire() as c:
        try:
            await c.execute("INSERT INTO attendance(uid,date) VALUES($1,$2)",uid,ds)
            await ctx.reply("✅ 출근 완료! 오늘도 힘내요!")
        except:
            await ctx.reply("이미 출근했어요 🕐")

@bot.event
async def on_message(msg:discord.Message):
    if msg.author.bot: return
    cid=msg.channel.id; uid=str(msg.author.id)
    await ensure_user(uid)
    ch=CHANNEL_POINTS.get(cid)
    if not ch: return await bot.process_commands(msg)
    if ch["image_only"]:
        has_img=any(a.content_type and a.content_type.startswith("image/") for a in msg.attachments)
        if not has_img: return await bot.process_commands(msg)
    ds=logical_date_str(now_kst())
    cap=None
    try:cap=int(os.environ.get("GLOBAL_DAILY_CAP")) if os.environ.get("GLOBAL_DAILY_CAP") else None
    except:cap=None
    added,new=await add_activity(uid,ds,cid,cap)
    if added and new:
        await msg.author.send(f"🎉 이번주 {max(new)}점 달성! 꾸준함 최고예요 🕊️")
    await bot.process_commands(msg)

@bot.command(name="보고서")
async def report(ctx):
    uid=str(ctx.author.id)
    await ensure_user(uid)
    ref=logical_date_from_dt(now_kst())
    async with DB_POOL.acquire() as c:
        att=(await c.fetchval("SELECT COUNT(*) FROM attendance WHERE uid=$1",uid)) or 0
    total=await db_get_week_total(uid,ref)
    remain=max(0,WEEKLY_BEST_THRESHOLD-total)
    msg=f"🌼 {ctx.author.display_name}님 주간요약\n출근 {att}회, 점수 {total}점\n"
    msg+="🎉 우수사원 달성!" if remain==0 else f"✨ {remain}점 남았어요!"
    grid=await week_grid(uid,ref)
    file=discord.File(render_card(ctx.author.display_name,att,total,grid),"report.png")
    await ctx.reply(msg,file=file)

def is_admin(m): 
    try: return m.guild_permissions.manage_guild
    except:return False

# ========= 관리자 리포트 (주간/월간) =========
async def db_all_users_week_total(ref_date: datetime.date) -> List[Tuple[str, int]]:
    start, end = get_week_range(ref_date)
    async with DB_POOL.acquire() as conn:
        rows = await conn.fetch("""
            SELECT uid, COALESCE(SUM(total), 0) AS s
            FROM activity
            WHERE date >= $1 AND date <= $2
            GROUP BY uid
            ORDER BY s DESC
        """, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    return [(r["uid"], int(r["s"])) for r in rows]

async def db_all_users_month_total(year: int, month: int) -> List[Tuple[str, int]]:
    prefix = f"{year:04d}-{month:02d}"
    async with DB_POOL.acquire() as conn:
        rows = await conn.fetch("""
            SELECT uid, COALESCE(SUM(total), 0) AS s
            FROM activity
            WHERE date LIKE $1 || '%'
            GROUP BY uid
            ORDER BY s DESC
        """, prefix)
    return [(r["uid"], int(r["s"])) for r in rows]

@bot.command(name="PP보고서")
async def cmd_pp_report(ctx, 기간: str = None, *args):
    if not is_admin(ctx.author):
        return await ctx.reply("이 명령어는 관리자만 사용할 수 있어요.")
    if 기간 not in ("주간", "월간"):
        return await ctx.reply("사용법: `!PP보고서 주간` 또는 `!PP보고서 월간 10월`")

    # 주간 리포트
    if 기간 == "주간":
        today = logical_date_from_dt(now_kst())
        start, end = get_week_range(today)
        pairs = await db_all_users_week_total(today)
        csv_buf = io.StringIO(); writer = csv.writer(csv_buf)
        writer.writerow(["사용자명", "사용자ID", "주간점수"])
        display_rows = []
        for uid, sc in pairs:
            try:
                member = await ctx.guild.fetch_member(int(uid))
                name = member.display_name
            except:
                name = uid
            writer.writerow([name, uid, sc]); display_rows.append((name, sc))
        csv_bytes = io.BytesIO(csv_buf.getvalue().encode("utf-8"))
        y, w, _ = today.isocalendar()
        filename = f"weekly_report_{today.year}-{today.month:02d}-Week{w}.csv"
        top20 = display_rows[:20]
        header = f"📊 이번주 활동 순위 ({start.month}월 {start.day}일 ~ {end.month}월 {end.day}일)\n" + "-"*40
        lines = [f"{i+1:>2}️⃣ {n} — {s}점" for i,(n,s) in enumerate(top20)]
        body = "\n".join(lines) if lines else "데이터가 없어요 😅"
        footer = "-"*40 + "\n📎 CSV 첨부 (유저명+ID 포함)"
        return await ctx.reply(f"{header}\n{body}\n{footer}", file=discord.File(fp=csv_bytes, filename=filename))

    # 월간 리포트
    if 기간 == "월간":
        today = logical_date_from_dt(now_kst())
        if args and len(args) >= 1:
            raw = args[0].strip()
            try: m = int(raw.replace("월","")); year = today.year
            except: return await ctx.reply("사용법: `!PP보고서 월간 10월` 처럼 숫자+월 형태로 입력해줘요!")
        else:
            m = today.month; year = today.year
        pairs = await db_all_users_month_total(year, m)
        csv_buf = io.StringIO(); writer = csv.writer(csv_buf)
        writer.writerow(["사용자명", "사용자ID", "월간점수"])
        display_rows = []
        for uid, sc in pairs:
            try:
                member = await ctx.guild.fetch_member(int(uid))
                name = member.display_name
            except:
                name = uid
            writer.writerow([name, uid, sc]); display_rows.append((name, sc))
        csv_bytes = io.BytesIO(csv_buf.getvalue().encode("utf-8"))
        filename = f"monthly_report_{year}-{m:02d}월.csv"
        top20 = display_rows[:20]
        header = f"📅 {year}년 {m}월 활동 순위\n" + "-"*40
        lines = [f"{i+1:>2}️⃣ {n} — {s}점" for i,(n,s) in enumerate(top20)]
        body = "\n".join(lines) if lines else "데이터가 없어요 😅"
        footer = "-"*40 + "\n📎 CSV 첨부 (유저명+ID 포함)"
        return await ctx.reply(f"{header}\n{body}\n{footer}", file=discord.File(fp=csv_bytes, filename=filename))

# ========= 백업 루프 =========
async def daily_backup_loop():
    while True:
        now=now_kst()
        target=now.replace(hour=6,minute=0,second=0,microsecond=0)
        if now>=target:target+=datetime.timedelta(days=1)
        await asyncio.sleep((target-now).total_seconds())
        snap={"generated_at":now.isoformat(),"users":[]}
        async with DB_POOL.acquire() as c:
            us=await c.fetch("SELECT uid FROM users")
            for u in us:
                uid=u["uid"]
                att=await c.fetch("SELECT date FROM attendance WHERE uid=$1",uid)
                act=await c.fetch("SELECT date,total,by_channel FROM activity WHERE uid=$1",uid)
                snap["users"].append({
                    "uid":uid,
                    "attendance":[a["date"] for a in att],
                    "activity":[{"date":a["date"],"total":a["total"],"by_channel":a["by_channel"]} for a in act]
                })
        buf=io.BytesIO(json.dumps(snap,ensure_ascii=False,indent=2).encode())
        name=f"snapshot_{logical_date_str(now)}.json"
        if BACKUP_CHANNEL_ID:
            ch=bot.get_channel(BACKUP_CHANNEL_ID)
            if ch: await ch.send("🧷 자동 백업 (06시)",file=discord.File(buf,filename=name))
        print("✅ 백업 완료")

if __name__=="__main__":
    if not DISCORD_TOKEN or not DB_URL:
        print("❌ 환경변수 누락 (DISCORD_BOT_TOKEN, DATABASE_URL)")
    else:
        bot.run(DISCORD_TOKEN)
