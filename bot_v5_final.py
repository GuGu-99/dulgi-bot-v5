# -*- coding: utf-8 -*-
# 둘기봇 v5.2 — 통합 완전판 (JSON 저장 / DM 보고서 / 잔디 타일 / 6시 기준 / 수동·자동 백업 / 관리자 리포트)
# - !출근, !보고서 → DM 전송
# - 잔디 타일(주간 + 5x4 월간) 텍스트 복귀
# - 오전 6시(KST) 자동 백업 + !백업 시 수동 백업 파일을 백업 채널 업로드
# - 관리자 리포트 : !PP보고서 주간 / !PP보고서 월간 YY.MM (상위20 + CSV)
# - 점수 적립: 채널별 포인트 + 일일 채널 최대치 + (옵션) 글로벌 일일 상한
# - ‘다-그렸어요’ 특수규칙: 이미지 또는 링크 포함 시 점수 인정
# - 주간 50점 단위 달성 시 DM 축하(격려문구 랜덤은 제거 요청대로 미포함)

import os, io, csv, json, random, asyncio, datetime, pytz
from typing import Dict, Tuple, List
from flask import Flask
from threading import Thread

import discord
from discord.ext import commands

# ========= 기본 설정 =========
KST = pytz.timezone("Asia/Seoul")
LOGICAL_DAY_START_HOUR = 6  # 하루 시작: 오전 6시
DATA_FILE = "data.json"
BACKUP_FILE = "data_backup.json"

# Render 환경변수 (선택: 백업 채널, 글로벌 일일 상한)
BACKUP_CHANNEL_ID = int(os.environ.get("BACKUP_CHANNEL_ID", "0"))
GLOBAL_DAILY_CAP_ENV = os.environ.get("GLOBAL_DAILY_CAP")

# 채널 점수체계 (name, points, daily_max, image_only)
CHANNEL_POINTS = {
    1423170386811682908: {"name": "일일-그림보고", "points": 6, "daily_max": 6, "image_only": True},
    1423172691724079145: {"name": "자유채팅판", "points": 1, "daily_max": 4, "image_only": False},
    1423359059566006272: {"name": "정보-공모전", "points": 1, "daily_max": 1, "image_only": False},
    1423170949477568623: {"name": "정보-그림꿀팁", "points": 1, "daily_max": 1, "image_only": False},
    1423242322665148531: {"name": "고민상담", "points": 1, "daily_max": 1, "image_only": False},
    1423359791287242782: {"name": "출퇴근기록", "points": 4, "daily_max": 4, "image_only": False},
    1423171509752434790: {"name": "다-그렸어요", "points": 5, "daily_max": 5, "image_only": True},  # 특수: 이미지 or 링크
}

WEEKLY_BEST_THRESHOLD = 60
MONTHLY_BEST_THRESHOLD = 200

# ========= 시간 유틸 (06:00 기준 날짜) =========
def now_kst() -> datetime.datetime:
    return datetime.datetime.now(KST)

def logical_date_from_dt(dt: datetime.datetime) -> datetime.date:
    if dt.hour < LOGICAL_DAY_START_HOUR:
        dt -= datetime.timedelta(days=1)
    return dt.date()

def logical_date_str_from_now() -> str:
    return logical_date_from_dt(now_kst()).strftime("%Y-%m-%d")

# ========= 주/월 계산 =========
def get_week_range_from_date_obj(d: datetime.date) -> Tuple[datetime.date, datetime.date]:
    start = d - datetime.timedelta(days=d.weekday())
    end = start + datetime.timedelta(days=6)
    return start, end

def week_key(d: datetime.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"

# ========= 데이터 유틸(JSON) =========
def load_data(path: str = DATA_FILE) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except Exception:
                return {}
    return {}

def save_data(data: dict, path: str = DATA_FILE):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def ensure_user(data: dict, uid: str):
    if "config" not in data:
        data["config"] = {}
    if "users" not in data:
        data["users"] = {}
    if uid not in data["users"]:
        data["users"][uid] = {
            "attendance": [],
            "activity": {},    # date_str -> {"total": int, "by_channel": {cid: pts}}
            "notified": {}     # week_key -> [50,100,...]
        }

# ========= 통계 로직 =========
def weekly_attendance_count_logic(data: Dict, uid: str, ref_date: datetime.date) -> int:
    ensure_user(data, uid)
    start, end = get_week_range_from_date_obj(ref_date)
    cnt = 0
    for ds in data["users"][uid]["attendance"]:
        try:
            ds_date = datetime.datetime.strptime(ds, "%Y-%m-%d").date()
            if start <= ds_date <= end:
                cnt += 1
        except Exception:
            pass
    return cnt

def weekly_activity_points_logic(data: Dict, uid: str, ref_date: datetime.date) -> Tuple[int, Dict[str, int]]:
    ensure_user(data, uid)
    start, end = get_week_range_from_date_obj(ref_date)
    total = 0
    breakdown: Dict[str, int] = {}
    for ds, rec in data["users"][uid]["activity"].items():
        try:
            ds_date = datetime.datetime.strptime(ds, "%Y-%m-%d").date()
        except Exception:
            continue
        if start <= ds_date <= end:
            t = rec.get("total", 0)
            total += t
            for cid, pts in rec.get("by_channel", {}).items():
                breakdown[cid] = breakdown.get(cid, 0) + pts
    return total, breakdown

def monthly_activity_points_logic(data: Dict, uid: str, year: int, month: int) -> int:
    ensure_user(data, uid)
    prefix = f"{year:04d}-{month:02d}"
    s = 0
    for ds, rec in data["users"][uid]["activity"].items():
        if ds.startswith(prefix):
            s += rec.get("total", 0)
    return s

# ========= 점수 추가 로직 =========
def add_activity_logic(
    data: Dict,
    uid: str,
    date_str: str,
    channel_id: int,
    channel_points_map: Dict,
    global_daily_cap: int = None
) -> Tuple[bool, List[int]]:
    """점수 추가 & 50점 단위 축하 알림(이번 주)"""
    ensure_user(data, uid)
    conf = channel_points_map.get(channel_id)
    if not conf:
        return False, []

    points = conf["points"]
    ch_max = conf["daily_max"]

    user = data["users"][uid]
    if date_str not in user["activity"]:
        user["activity"][date_str] = {"total": 0, "by_channel": {}}
    today_rec = user["activity"][date_str]
    ckey = str(channel_id)
    prev_by = today_rec["by_channel"].get(ckey, 0)

    # per-channel daily cap
    if prev_by + points > ch_max:
        return False, []

    # global cap (옵션)
    if global_daily_cap is not None and today_rec["total"] + points > int(global_daily_cap):
        return False, []

    # 이전 주간 총점
    d = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    wkey = week_key(d)
    prev_week_total = 0
    for ds, rec in user["activity"].items():
        try:
            ds_date = datetime.datetime.strptime(ds, "%Y-%m-%d").date()
        except Exception:
            continue
        if week_key(ds_date) == wkey:
            prev_week_total += rec.get("total", 0)

    # 점수 반영
    today_rec["by_channel"][ckey] = prev_by + points
    today_rec["total"] += points

    # 새로운 주간 총점
    new_week_total = 0
    for ds, rec in user["activity"].items():
        try:
            ds_date = datetime.datetime.strptime(ds, "%Y-%m-%d").date()
        except Exception:
            continue
        if week_key(ds_date) == wkey:
            new_week_total += rec.get("total", 0)

    # 50점 단위 축하 알림
    notified_levels = set(user["notified"].get(wkey, []))
    prev_level = prev_week_total // 50
    new_level = new_week_total // 50
    newly = []
    if new_level > prev_level:
        for lv in range(prev_level + 1, new_level + 1):
            milestone = lv * 50
            if milestone not in notified_levels:
                newly.append(milestone)
                notified_levels.add(milestone)
        user["notified"][wkey] = sorted(list(notified_levels))

    return True, newly

# ========= 잔디 타일 =========
def get_week_progress(data: Dict, uid: str, ref_date: datetime.date, daily_goal: int = 10) -> str:
    start, _ = get_week_range_from_date_obj(ref_date)
    labels = ["월", "화", "수", "목", "금", "토", "일"]
    blocks = []
    cur = start
    for _ in range(7):
        ds = cur.strftime("%Y-%m-%d")
        pts = data["users"][uid]["activity"].get(ds, {}).get("total", 0)
        blocks.append("🟩" if pts >= daily_goal else "⬜")
        cur += datetime.timedelta(days=1)
    return " ".join(labels) + "\n" + " ".join(blocks)

def get_month_grid_5x4(data: Dict, uid: str, ref_date: datetime.date, daily_goal: int = 10) -> str:
    first = ref_date.replace(day=1)
    next_month = (first.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    month_days = (next_month - datetime.timedelta(days=1)).day
    cells = []
    for day in range(1, 21):  # 1~20일만
        if day > month_days:
            cells.append("  ")
            continue
        ds = first.replace(day=day).strftime("%Y-%m-%d")
        pts = data["users"][uid]["activity"].get(ds, {}).get("total", 0)
        cells.append("🟩" if pts >= daily_goal else "⬜")
    rows = []
    for r in range(4):
        rows.append(" ".join(cells[r*5:(r+1)*5]))
    return "월간 활동 (1~20일 기준, 초록=달성)\n" + "\n".join(rows)

# ========= 백업 =========
def backup_now() -> bool:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = f.read()
        with open(BACKUP_FILE, "w", encoding="utf-8") as f:
            f.write(data)
        return True
    return False

async def schedule_daily_backup_loop():
    # 매일 06:00 KST에 자동 백업 + 백업 채널 업로드
    while True:
        now = now_kst()
        target = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if now >= target:
            target += datetime.timedelta(days=1)
        await asyncio.sleep(max(1, int((target - now).total_seconds())))
        ok = backup_now()
        if ok:
            print("✅ Daily backup created at 06:00 KST")
            try:
                buf = io.BytesIO(json.dumps(load_data(), ensure_ascii=False, indent=2).encode())
                name = f"snapshot_{now_kst().strftime('%Y%m%d_%H%M')}.json"
                if BACKUP_CHANNEL_ID:
                    ch = bot.get_channel(BACKUP_CHANNEL_ID)
                    if ch:
                        await ch.send("🧷 자동 백업 (06시)", file=discord.File(buf, filename=name))
            except Exception as e:
                print(f"Backup upload error: {e}")

# ========= Discord & Flask =========
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

data_store = load_data()

# Flask keep-alive (Render)
app = Flask(__name__)
@app.route("/")
def home():
    return "Bot is alive!"

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))

def keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()

# ========= 권한 체크 =========
def is_admin(member: discord.Member) -> bool:
    try:
        return member.guild_permissions.manage_guild
    except Exception:
        return False

# ========= 이벤트 & 명령어 =========
@bot.event
async def on_ready():
    print(f"✅ 로그인 완료: {bot.user}")
    keep_alive()
    bot.backup_task = asyncio.create_task(schedule_daily_backup_loop())

@bot.command(name="출근")
async def check_in(ctx):
    uid = str(ctx.author.id)
    today_str = logical_date_str_from_now()
    ensure_user(data_store, uid)
    if today_str in data_store["users"][uid]["attendance"]:
        try:
            await ctx.author.send("이미 출근을 완료했습니다 🕐")
        except:
            await ctx.reply("이미 출근을 완료했습니다 🕐")
        return
    data_store["users"][uid]["attendance"].append(today_str)
    save_data(data_store)
    try:
        await ctx.author.send("✅ 출근 완료! 오늘도 힘내요!")
    except:
        await ctx.reply("✅ 출근 완료! 오늘도 힘내요!")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    cid = message.channel.id
    uid = str(message.author.id)
    ensure_user(data_store, uid)

    ch_conf = CHANNEL_POINTS.get(cid)
    if ch_conf is None:
        await bot.process_commands(message)
        return

    # 특수규칙: ‘다-그렸어요’(cid=1423171509752434790)는 이미지 또는 링크 포함 시 인정
    special_channel = 1423171509752434790
    countable = True
    if cid == special_channel:
        has_link = ("http://" in message.content) or ("https://" in message.content)
        has_attachment = any(a for a in message.attachments)
        countable = has_link or has_attachment
    else:
        # image_only면 이미지 첨부 필요
        if ch_conf.get("image_only"):
            has_image = any(a.content_type and a.content_type.startswith("image/") for a in message.attachments)
            countable = has_image

    if not countable:
        await bot.process_commands(message)
        return

    # 글로벌 일일 상한 (환경변수 또는 data_store["config"]["global_daily_cap"])
    global_cap = None
    try:
        if GLOBAL_DAILY_CAP_ENV:
            global_cap = int(GLOBAL_DAILY_CAP_ENV)
        else:
            cfg_cap = data_store.get("config", {}).get("global_daily_cap")
            global_cap = int(cfg_cap) if cfg_cap is not None else None
    except Exception:
        global_cap = None

    today_str = logical_date_str_from_now()
    added, newly = add_activity_logic(
        data_store, uid, today_str, cid, CHANNEL_POINTS, global_daily_cap=global_cap
    )
    if added:
        save_data(data_store)
        # 50점 단위 축하 DM
        if newly:
            try:
                # 최신 주간 총점 계산
                wtotal, _ = weekly_activity_points_logic(data_store, uid, logical_date_from_dt(now_kst()))
                pick = f"🎉 이번주 {max(newly)}점 달성! (현재 주간 합계: {wtotal}점)"
                await message.author.send(pick)
            except Exception:
                pass

    await bot.process_commands(message)

@bot.command(name="보고서")
async def report_personal(ctx):
    uid = str(ctx.author.id)
    today = logical_date_from_dt(now_kst())
    ensure_user(data_store, uid)

    att = weekly_attendance_count_logic(data_store, uid, today)
    pts, breakdown = weekly_activity_points_logic(data_store, uid, today)

    # 채널명으로 변환
    bd_lines = []
    for cid, v in breakdown.items():
        try:
            cid_int = int(cid)
        except Exception:
            cid_int = None
        nm = None
        if cid_int and cid_int in CHANNEL_POINTS:
            nm = CHANNEL_POINTS[cid_int]["name"]
        else:
            nm = str(cid)
        bd_lines.append(f"{nm}: {v}")
    bd_read = ", ".join(bd_lines) if bd_lines else "없음"

    remain = max(0, WEEKLY_BEST_THRESHOLD - pts)

    msg = (
        f"🌼 {ctx.author.display_name}님, 이번 주 활동 요약이에요!\n\n"
        f"🕐 출근 횟수: {att}회\n"
        f"💬 활동 점수: {pts}점\n"
        f"📂 활동 채널별: {bd_read}\n\n"
    )
    if remain > 0:
        msg += f"✨ 우수사원까지 {remain}점 남았어요! 💪\n"
    else:
        msg += "🎉 축하드려요! 이번 주 우수사원 기준을 달성했어요! 멋져요 💖\n"

    # 잔디 타일 (주간 + 월간 5x4)
    msg += "\n📊 이번주 활동 현황:\n" + get_week_progress(data_store, uid, today, daily_goal=10) + "\n"
    msg += "\n" + get_month_grid_5x4(data_store, uid, today, daily_goal=10) + "\n"

    try:
        await ctx.author.send(msg)
    except:
        await ctx.reply("DM을 보낼 수 없습니다! DM 허용을 켜주세요 🕊️")

# ====== 관리자: 수동 백업 ======
@bot.command(name="백업")
async def cmd_backup(ctx):
    if not is_admin(ctx.author):
        return await ctx.reply("이 명령어는 관리자만 사용할 수 있어요.")
    ok = backup_now()
    if ok:
        # 현재 data.json 스냅샷을 백업 채널로 업로드
        try:
            buf = io.BytesIO(json.dumps(load_data(), ensure_ascii=False, indent=2).encode())
            name = f"manual_backup_{now_kst().strftime('%Y%m%d_%H%M')}.json"
            if BACKUP_CHANNEL_ID:
                ch = bot.get_channel(BACKUP_CHANNEL_ID)
                if ch:
                    await ch.send("🧷 수동 백업 실행됨", file=discord.File(buf, filename=name))
        except Exception as e:
            print(f"Manual backup upload error: {e}")
        await ctx.reply("✅ 수동 백업 완료! (백업 채널 업로드)")
    else:
        await ctx.reply("⚠️ 백업할 데이터가 없어요.")

# ====== 관리자 리포트: 주간/월간 ======
def all_users_week_total(data: Dict, ref_date: datetime.date) -> List[Tuple[str, int]]:
    ret = []
    for uid in data.get("users", {}):
        total, _ = weekly_activity_points_logic(data, uid, ref_date)
        ret.append((uid, total))
    ret.sort(key=lambda x: x[1], reverse=True)
    return ret

def all_users_month_total(data: Dict, year: int, month: int) -> List[Tuple[str, int]]:
    ret = []
    for uid in data.get("users", {}):
        total = monthly_activity_points_logic(data, uid, year, month)
        ret.append((uid, total))
    ret.sort(key=lambda x: x[1], reverse=True)
    return ret

@bot.command(name="PP보고서")
async def cmd_pp_report(ctx, 기간: str = None, *args):
    """관리자 전용: !PP보고서 주간  /  !PP보고서 월간 YY.MM  (또는 10월 허용)"""
    if not is_admin(ctx.author):
        return await ctx.reply("이 명령어는 관리자만 사용할 수 있어요.")
    if 기간 not in ("주간", "월간"):
        return await ctx.reply("사용법: `!PP보고서 주간` 또는 `!PP보고서 월간 YY.MM`")

    if 기간 == "주간":
        today = logical_date_from_dt(now_kst())
        start, end = get_week_range_from_date_obj(today)
        pairs = all_users_week_total(data_store, today)  # [(uid, total), ...] desc

        # CSV (유저명+ID+점수)
        csv_buf = io.StringIO()
        writer = csv.writer(csv_buf)
        writer.writerow(["순위", "사용자명", "사용자ID", "주간점수"])
        display_rows = []
        for rank, (uid, sc) in enumerate(pairs, start=1):
            try:
                member = await ctx.guild.fetch_member(int(uid))
                name = member.display_name
            except Exception:
                name = uid
            writer.writerow([rank, name, uid, sc])
            display_rows.append((name, sc))

        csv_bytes = io.BytesIO(csv_buf.getvalue().encode("utf-8"))
        y, w, _ = today.isocalendar()
        filename = f"weekly_report_{today.year}-{today.month:02d}-Week{w}.csv"

        # 텍스트 상위 20명
        top20 = display_rows[:20]
        header = f"📊 이번주 활동 순위 ({start.month}월 {start.day}일 ~ {end.month}월 {end.day}일)\n" + "-"*40
        lines = [f"{i+1:>2}️⃣ {n} — {s}점" for i, (n, s) in enumerate(top20)]
        body = "\n".join(lines) if lines else "데이터가 없어요 😅"
        footer = "-"*40 + "\n📎 CSV 첨부 (유저명+ID 포함)"

        return await ctx.reply(f"{header}\n{body}\n{footer}", file=discord.File(fp=csv_bytes, filename=filename))

    # ====== 월간 ======
    if 기간 == "월간":
        today = logical_date_from_dt(now_kst())

        # YY.MM 또는 YYYY.MM 또는 "10월" 모두 허용
        target_year, target_month = today.year, today.month
        if args and len(args) >= 1:
            raw = args[0].strip()
            try:
                if "월" in raw:
                    # "10월" 형태
                    target_month = int(raw.replace("월", ""))
                elif "." in raw:
                    # YY.MM 또는 YYYY.MM
                    y_s, m_s = raw.split(".")
                    if len(y_s) == 2:
                        target_year = int("20" + y_s)
                    else:
                        target_year = int(y_s)
                    target_month = int(m_s)
                else:
                    target_month = int(raw)
            except Exception:
                return await ctx.reply("형식 오류: 예) `!PP보고서 월간 25.09` 또는 `!PP보고서 월간 10월`")

        pairs = all_users_month_total(data_store, target_year, target_month)

        # CSV
        csv_buf = io.StringIO()
        writer = csv.writer(csv_buf)
        writer.writerow(["순위", "사용자명", "사용자ID", "월간점수"])
        display_rows = []
        for rank, (uid, sc) in enumerate(pairs, start=1):
            try:
                member = await ctx.guild.fetch_member(int(uid))
                name = member.display_name
            except Exception:
                name = uid
            writer.writerow([rank, name, uid, sc])
            display_rows.append((name, sc))

        csv_bytes = io.BytesIO(csv_buf.getvalue().encode("utf-8"))
        filename = f"monthly_report_{target_year}-{target_month:02d}.csv"

        # 텍스트 상위 20명
        top20 = display_rows[:20]
        header = f"📅 {target_year}년 {target_month}월 활동 순위\n" + "-"*40
        lines = [f"{i+1:>2}️⃣ {n} — {s}점" for i, (n, s) in enumerate(top20)]
        body = "\n".join(lines) if lines else "데이터가 없어요 😅"
        footer = "-"*40 + "\n📎 CSV 첨부 (유저명+ID 포함)"

        return await ctx.reply(f"{header}\n{body}\n{footer}", file=discord.File(fp=csv_bytes, filename=filename))

# ========= 시작 =========
if __name__ == "__main__":
    TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
    if not TOKEN:
        print("❌ DISCORD_BOT_TOKEN 누락")
    else:
        bot.run(TOKEN)
