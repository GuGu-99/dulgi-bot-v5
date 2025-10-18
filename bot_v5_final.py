# -*- coding: utf-8 -*-
# 둘기봇 v5.5.1 — Progress System + DM Button + Safe Backup (Render Starter Ready)
# 변경점:
# - !출근 실행 시 출근 처리 + 개인 보고서(월간 제외) DM 동시 발송
# - !보고서 실행 시 개인 보고서(주간 + 월간 7x4) DM 발송
# - 하루 10점/주간 50점 달성 시 축하 DM (중복 방지)
# - 타일(🟩/⬜) 기준: 하루 누적 점수 >= DAILY_GOAL_POINTS (기본 10)
# - 기존 기능(백업/복원/자동백업/관리자 리포트/데이터 영구 저장) 유지

import os
import io
import csv
import json
import random
import asyncio
import datetime
import pytz
import aiohttp
from typing import Dict, Tuple, List
from flask import Flask
from threading import Thread

import discord
from discord.ext import commands
from discord.ui import View, Button

# ========= 기본 설정 =========
KST = pytz.timezone("Asia/Seoul")

# Persistent Disk 경로 (Render Starter 플랜)
BASE_PATH = "/opt/render/project/data"
os.makedirs(BASE_PATH, exist_ok=True)
DATA_FILE = os.path.join(BASE_PATH, "data.json")
BACKUP_FILE = os.path.join(BASE_PATH, "data_backup.json")

# (초기 1회) 구버전 위치에서 마이그레이션
OLD_DATA_FILE = "/opt/render/project/src/data.json"
if os.path.exists(OLD_DATA_FILE) and not os.path.exists(DATA_FILE):
    try:
        os.system(f"cp {OLD_DATA_FILE} {DATA_FILE}")
        print("✅ 이전 data.json을 Disk로 자동 마이그레이션 완료.")
    except Exception as e:
        print("⚠️ 마이그레이션 실패:", e)

# 서버 버튼 링크(서버로 돌아가기)
SERVER_URL = "https://discord.com/channels/1310854848442269767"

# 백업 업로드 채널(필요시 교체)
BACKUP_CHANNEL_ID = 1427608696547967026  # 🔧 실제 백업 채널 ID로 교체하세요

# 채널 점수 체계
CHANNEL_POINTS = {
    1423170386811682908: {"name": "일일-그림보고", "points": 6, "daily_max": 6, "image_only": True},
    1423172691724079145: {"name": "자유채팅판", "points": 1, "daily_max": 4, "image_only": False},
    1423359059566006272: {"name": "정보-공모전", "points": 1, "daily_max": 1, "image_only": False},
    1423170949477568623: {"name": "정보-그림꿀팁", "points": 1, "daily_max": 1, "image_only": False},
    1423242322665148531: {"name": "고민상담", "points": 1, "daily_max": 1, "image_only": False},
    1423359791287242782: {"name": "출퇴근기록", "points": 4, "daily_max": 4, "image_only": False},
    1423171509752434790: {"name": "다-그렸어요", "points": 5, "daily_max": 5, "image_only": True},  # 이미지 또는 링크 허용
}

# 우수 기준(기존 주간 60/월간 200 유지 + 신규 알림: 하루 10 / 주간 50)
WEEKLY_BEST_THRESHOLD = 60
MONTHLY_BEST_THRESHOLD = 200
DAILY_GOAL_POINTS = 10   # 🟩 타일 기준 & '하루 목표 달성' DM 기준
WEEK_GOAL_POINTS = 50    # '이주의 우수사원' DM 기준

# ========= 데이터 유틸 =========
def load_data(path: str = DATA_FILE) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_data(data: dict, path: str = DATA_FILE):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def ensure_user(data: dict, uid: str):
    """향후 확장 대비 기본 구조 보장"""
    if "users" not in data:
        data["users"] = {}
    if uid not in data["users"]:
        data["users"][uid] = {}
    user = data["users"][uid]
    user.setdefault("attendance", [])
    user.setdefault("activity", {})
    user.setdefault("notified", {})  # 축하 알림 기록
    # 미래 확장(레벨/뱃지 등)
    user.setdefault("level", 1)
    user.setdefault("exp", 0)
    user.setdefault("rank_title", None)
    user.setdefault("badges", [])
    data["users"][uid] = user

def logical_date_str_from_now() -> str:
    """한국시간 오전 6시를 하루 경계로 사용하는 '논리적 날짜' 문자열"""
    now = datetime.datetime.now(KST)
    logical = now - datetime.timedelta(hours=6)
    return logical.strftime("%Y-%m-%d")

def get_week_range(d: datetime.date) -> Tuple[datetime.date, datetime.date]:
    start = d - datetime.timedelta(days=d.weekday())
    end = start + datetime.timedelta(days=6)
    return start, end

def week_key(d: datetime.date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"

def add_activity_logic(
    data: dict,
    uid: str,
    date_str: str,
    channel_id: int,
    channel_points_map: Dict[int, dict]
) -> bool:
    """일자/채널별 점수 반영(채널 일일 상한 준수)"""
    ensure_user(data, uid)
    conf = channel_points_map.get(channel_id)
    if not conf:
        return False
    points, ch_max = conf["points"], conf["daily_max"]
    user = data["users"][uid]
    if date_str not in user["activity"]:
        user["activity"][date_str] = {"total": 0, "by_channel": {}}
    today_rec = user["activity"][date_str]
    ckey = str(channel_id)
    prev = today_rec["by_channel"].get(ckey, 0)
    if prev + points > ch_max:
        return False
    today_rec["by_channel"][ckey] = prev + points
    today_rec["total"] += points
    return True

# ========= 시각화 =========
def get_week_progress(data: dict, uid: str, ref_date: datetime.date, daily_goal: int = DAILY_GOAL_POINTS) -> str:
    start, _ = get_week_range(ref_date)
    labels = ["월 ", "화 ", "수 ", "목 ", "금 ", "토 ", "일"]
    blocks = []
    cur = start
    for _ in range(7):
        ds = cur.strftime("%Y-%m-%d")
        pts = data["users"][uid]["activity"].get(ds, {}).get("total", 0)
        blocks.append("🟩" if pts >= daily_goal else "⬜")
        cur += datetime.timedelta(days=1)
    return " ".join(labels) + "\n" + " ".join(blocks)

def get_month_grid_7x4(data: dict, uid: str, ref_date: datetime.date, daily_goal: int = DAILY_GOAL_POINTS) -> str:
    """월간 7x4 타일(1~28일)"""
    first = ref_date.replace(day=1)
    cells = []
    for day in range(1, 29):
        ds = first.replace(day=day).strftime("%Y-%m-%d")
        pts = data["users"][uid]["activity"].get(ds, {}).get("total", 0)
        cells.append("🟩" if pts >= daily_goal else "⬜")
    rows = [" ".join(cells[r*7:(r+1)*7]) for r in range(4)]
    return "월간 활동 (1~28일 기준)\n" + "\n".join(rows)

# ========= 합계 계산 =========
def weekly_total_for_user(data: dict, uid: str, ref_date: datetime.date) -> int:
    ensure_user(data, uid)
    start, end = get_week_range(ref_date)
    return sum(
        rec.get("total", 0)
        for ds, rec in data["users"][uid]["activity"].items()
        if start <= datetime.datetime.strptime(ds, "%Y-%m-%d").date() <= end
    )

def monthly_total_for_user(data: dict, uid: str, year: int, month: int) -> int:
    ensure_user(data, uid)
    prefix = f"{year:04d}-{month:02d}"
    return sum(
        rec.get("total", 0)
        for ds, rec in data["users"][uid]["activity"].items()
        if ds.startswith(prefix)
    )

def all_users_week_total(data: dict, ref_date: datetime.date) -> List[Tuple[str, int]]:
    ret = []
    for uid in data.get("users", {}):
        ret.append((uid, weekly_total_for_user(data, uid, ref_date)))
    ret.sort(key=lambda x: x[1], reverse=True)
    return ret

def all_users_month_total(data: dict, year: int, month: int) -> List[Tuple[str, int]]:
    ret = []
    for uid in data.get("users", {}):
        ret.append((uid, monthly_total_for_user(data, uid, year, month)))
    ret.sort(key=lambda x: x[1], reverse=True)
    return ret

# ========= Discord & Flask =========
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

data_store = load_data()

app = Flask(__name__)
@app.route("/")
def home():
    return "Bot is alive!"
def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
def keep_alive():
    Thread(target=run_flask, daemon=True).start()

def is_admin(member: discord.Member) -> bool:
    try:
        return member.guild_permissions.manage_guild
    except Exception:
        return False

@bot.event
async def on_ready():
    print(f"✅ 로그인 완료: {bot.user}")
    keep_alive()
    bot.backup_task = asyncio.create_task(schedule_daily_backup_loop())

# ========= 공용 보고서 발송 함수 =========
async def send_personal_report(user: discord.User | discord.Member, include_month: bool = True):
    uid = str(user.id)
    today = datetime.datetime.now(KST).date()
    ensure_user(data_store, uid)
    user_data = data_store["users"][uid]

    today_str = logical_date_str_from_now()
    today_checked = "O" if today_str in user_data["attendance"] else "X"
    weekly_total = weekly_total_for_user(data_store, uid, today)

    week_map = get_week_progress(data_store, uid, today)

    # 기본(주간) 블록
    display_name = getattr(user, "display_name", None) or getattr(user, "name", "사용자")
    msg = (
        f"🌼 {display_name}님의 이번 주 활동 요약\n\n"
        f"오늘 출석 여부 : {today_checked}\n"
        f"이번주 획득 점수 : {weekly_total}점\n\n"
        f"📊 주간 활동:\n{week_map}"
    )

    # 월간 타일은 옵션
    if include_month:
        month_map = get_month_grid_7x4(data_store, uid, today)
        msg += f"\n\n{month_map}"

    server_button = Button(label="서버로 돌아가기 🏠", url=SERVER_URL)
    view = View(); view.add_item(server_button)
    await user.send(msg, view=view)

# ========= 출근 =========
@bot.command(name="출근")
async def check_in(ctx):
    uid = str(ctx.author.id)
    today_ds = logical_date_str_from_now()
    ensure_user(data_store, uid)
    user = data_store["users"][uid]

    if today_ds in user["attendance"]:
        server_button = Button(label="서버로 돌아가기 🏠", url=SERVER_URL)
        view = View(); view.add_item(server_button)
        return await ctx.author.send("이미 출근 완료 🕐\n매일 오전 6시에 초기화됩니다.", view=view)

    # 출근 기록 + 점수(+4) 반영
    user["attendance"].append(today_ds)
    add_activity_logic(data_store, uid, today_ds, 1423359791287242782, CHANNEL_POINTS)
    save_data(data_store)

    # 출근 완료 안내
    await ctx.author.send("✅ 출근 완료! (+4점) 오늘도 힘내요!")

    # 출근 후 개인 보고서(월간 제외) 자동 발송
    await send_personal_report(ctx.author, include_month=False)

# ========= 메시지 감지(점수 반영 + 목표 달성 DM) =========
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    cid = message.channel.id
    uid = str(message.author.id)
    today_ds = logical_date_str_from_now()
    ensure_user(data_store, uid)

    conf = CHANNEL_POINTS.get(cid)
    if conf is None:
        await bot.process_commands(message)
        return

    # 특수 채널: '다-그렸어요' = 링크 or 첨부파일(이미지/기타) 허용
    countable = True
    if cid == 1423171509752434790:
        has_link = "http://" in message.content or "https://" in message.content or "http" in message.content
        has_attach = len(message.attachments) > 0
        countable = has_link or has_attach
    else:
        if conf.get("image_only"):
            countable = any(a.content_type and a.content_type.startswith("image/") for a in message.attachments)

    if not countable:
        await bot.process_commands(message)
        return

    added = add_activity_logic(data_store, uid, today_ds, cid, CHANNEL_POINTS)
    if added:
        save_data(data_store)

        # === 목표 달성 축하 DM ===
        try:
            user_data = data_store["users"][uid]
            # 오늘 합계
            today_total = user_data["activity"][today_ds]["total"]
            # 주간 합계
            today = datetime.datetime.now(KST).date()
            w_total = weekly_total_for_user(data_store, uid, today)
            # 알림 중복 방지 키
            notified = user_data.setdefault("notified", {})
            daily_key = f"daily_{today_ds}"
            weekly_key = f"weekly_{week_key(today)}"

            # 하루 10점 달성
            if today_total >= DAILY_GOAL_POINTS and not notified.get(daily_key):
                week_map = get_week_progress(data_store, uid, today)
                dm = (
                    f"🌞 오늘 하루 목표({DAILY_GOAL_POINTS}점) 달성! 정말 수고했어요.\n"
                    f"내일도 꾸준히 채워나가봐요 💪\n\n"
                    f"📊 주간 활동:\n{week_map}"
                )
                await message.author.send(dm)
                notified[daily_key] = True

            # 주간 50점 달성
            if w_total >= WEEK_GOAL_POINTS and not notified.get(weekly_key):
                dm = (
                    f"🏆 이번 주 {w_total}점 달성! 이주의 우수사원이에요!\n"
                    f"다음 주도 잘 부탁드려요 ☀️"
                )
                await message.author.send(dm)
                notified[weekly_key] = True

            save_data(data_store)
        except Exception:
            pass

    await bot.process_commands(message)

# ========= 보고서(개인) =========
@bot.command(name="보고서")
async def report(ctx):
    await send_personal_report(ctx.author, include_month=True)

# ========= 백업/복원 =========
def backup_now() -> bool:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = f.read()
        with open(BACKUP_FILE, "w", encoding="utf-8") as f:
            f.write(data)
        return True
    return False

@bot.command(name="백업")
async def cmd_backup(ctx):
    if not is_admin(ctx.author):
        return await ctx.reply("관리자만 가능해요.")
    ok = backup_now()
    if ok:
        await ctx.reply("✅ 백업 완료! 백업 파일 업로드 중...")
        try:
            ch = bot.get_channel(BACKUP_CHANNEL_ID)
            if ch:
                await ch.send(
                    f"📦 [{datetime.datetime.now(KST).strftime('%Y-%m-%d %H:%M')}] 자동 백업 파일입니다.",
                    file=discord.File(BACKUP_FILE)
                )
        except Exception as e:
            await ctx.reply(f"⚠️ 업로드 중 오류: {e}")
    else:
        await ctx.reply("⚠️ 백업 실패")

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
        await ctx.reply("✅ 복원 완료! 기존 데이터 갱신됨")
    except Exception as e:
        await ctx.reply(f"⚠️ 복원 중 오류: {e}")

# ========= 자동 백업 루프 =========
async def schedule_daily_backup_loop():
    # 매일 06:00 KST
    while True:
        now = datetime.datetime.now(KST)
        next_backup = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if next_backup < now:
            next_backup += datetime.timedelta(days=1)
        await asyncio.sleep((next_backup - now).total_seconds())
        if backup_now():
            print("✅ Daily backup completed at 06:00 KST")
            try:
                ch = bot.get_channel(BACKUP_CHANNEL_ID)
                if ch:
                    await ch.send(
                        f"☀️ [{datetime.datetime.now(KST).strftime('%Y-%m-%d %H:%M')}] 오전 6시 자동 백업 완료!",
                        file=discord.File(BACKUP_FILE)
                    )
            except Exception as e:
                print("⚠️ 자동 백업 업로드 실패:", e)

# ========= 관리자 보고서 =========
@bot.command(name="PP보고서")
async def cmd_pp_report(ctx, 기간: str = None, *args):
    if not is_admin(ctx.author):
        return await ctx.reply("관리자만 가능해요.")
    if 기간 not in ("주간", "월간"):
        return await ctx.reply("사용법: `!PP보고서 주간` 또는 `!PP보고서 월간 N월`")

    today = datetime.datetime.now(KST).date()

    # --- 주간 보고서 ---
    if 기간 == "주간":
        pairs = all_users_week_total(data_store, today)
        csv_buf = io.StringIO()
        w = csv.writer(csv_buf)
        w.writerow(["닉네임", "ID", "주간점수"])
        text_lines = ["📊 **이번 주 상위 20명**", "```"]

        for i, (uid, sc) in enumerate(pairs[:20], start=1):
            try:
                member = await ctx.guild.fetch_member(int(uid))
                name = member.display_name
            except:
                name = uid
            w.writerow([name, uid, sc])
            text_lines.append(f"{i:>2}. {name:<20} | {sc:>4}점")

        text_lines.append("```")

        csv_bytes = io.BytesIO(csv_buf.getvalue().encode("utf-8"))
        start, end = get_week_range(today)
        header = f"📊 이번주 활동 순위 ({start.month}월 {start.day}일 ~ {end.month}월 {end.day}일)"
        await ctx.reply(
            header,
            file=discord.File(csv_bytes, f"weekly_report_{today.year}-W{today.isocalendar()[1]:02d}.csv"),
        )
        await ctx.send("\n".join(text_lines))
        return

    # --- 월간 보고서 ---
    if 기간 == "월간":
        target_year, target_month = today.year, today.month
        if args and len(args) >= 1:
            try:
                target_month = int(args[0].replace("월", ""))
            except:
                return await ctx.reply("사용법: `!PP보고서 월간 10월` 처럼 숫자+월 형태로 입력해줘!")
        pairs = all_users_month_total(data_store, target_year, target_month)

        csv_buf = io.StringIO()
        w = csv.writer(csv_buf)
        w.writerow(["닉네임", "ID", "월간점수"])
        text_lines = [f"📅 **{target_month}월 상위 20명**", "```"]

        for i, (uid, sc) in enumerate(pairs[:20], start=1):
            try:
                member = await ctx.guild.fetch_member(int(uid))
                name = member.display_name
            except:
                name = uid
            w.writerow([name, uid, sc])
            text_lines.append(f"{i:>2}. {name:<20} | {sc:>4}점")

        text_lines.append("```")

        csv_bytes = io.BytesIO(csv_buf.getvalue().encode("utf-8"))
        header = f"📅 {target_year}년 {target_month}월 활동 순위"
        await ctx.reply(header, file=discord.File(csv_bytes, f"monthly_report_{target_year}-{target_month:02d}.csv"))
        await ctx.send("\n".join(text_lines))
        return

# ========= 시작 =========
if __name__ == "__main__":
    TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("❌ DISCORD_BOT_TOKEN 환경변수가 설정되지 않았습니다.")


