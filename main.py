import os
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

import httpx
import pytz
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from linebot.v3.webhook import WebhookParser
from linebot.v3.webhooks import ImageMessageContent, MessageEvent, TextMessageContent

import calendar_service
import line_service
import nlp_parser
import state_store

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_USER_ID = os.getenv("LINE_USER_ID", "")
CRON_SECRET = os.getenv("CRON_SECRET", "")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tokyo")

parser = WebhookParser(CHANNEL_SECRET)
app = FastAPI(title="LINE Calendar Bot")

# Redisのキー定数
KEY_ALLDAY = "allday"
KEY_CALENDAR = "calendar"
KEY_TRUNCATED = "truncated"


def send_morning_report():
    """朝の予定通知（今日・今週）"""
    try:
        logger.info("朝の予定通知を送信中...")
        report = calendar_service.build_daily_report()
        line_service.push_message(LINE_USER_ID, report)
        logger.info("朝の予定通知を送信完了")
    except Exception as e:
        logger.error(f"朝の予定通知エラー: {e}")


def _ask_next_truncated_fix(user_id: str):
    """見切れ予定の修正を1件ずつ問い合わせる"""
    fixes = state_store.get_state(KEY_TRUNCATED, user_id)
    if not fixes:
        state_store.del_state(KEY_TRUNCATED, user_id)
        line_service.push_message(user_id, "✅ 全ての予定の修正が完了しました！")
        return
    fix = fixes[0]
    time_label = "終日" if fix["time_str"] == "終日" else fix["time_str"]
    line_service.push_message(
        user_id,
        f"✏️ 名前が見切れています\n「{fix['original_summary']}」\n"
        f"({fix['date']} {time_label})\n\n"
        f"正しい名前を入力してください。\n（スキップは「スキップ」）"
    )


def _process_calendar_image(message_id: str, user_id: str):
    """カレンダー画像を解析し確認メッセージを送信（バックグラウンド実行）"""
    try:
        token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
        with httpx.Client(timeout=60) as client:
            resp = client.get(
                f"https://api-data.line.me/v2/bot/message/{message_id}/content",
                headers={"Authorization": f"Bearer {token}"},
            )
        image_data = resp.content
        media_type = resp.headers.get("content-type", "image/jpeg").split(";")[0]

        events = nlp_parser.parse_calendar_image(image_data, media_type)
        if not events:
            line_service.push_message(user_id, "📅 予定が見つかりませんでした。TimeTreeのカレンダー画面を送ってください。")
            return

        state_store.set_state(KEY_CALENDAR, user_id, events)

        truncated_count = sum(1 for e in events if e.get("truncated"))
        lines = [f"📅 {len(events)}件の予定を検出しました。\n内容を確認して「はい」で登録、「キャンセル」で中止してください。\n"]
        for e in events[:20]:
            prefix = "⚠️" if e.get("truncated") else "・"
            if e.get("all_day"):
                lines.append(f"{prefix} {e['date']} {e['summary']}（終日）")
            else:
                lines.append(f"{prefix} {e['date']} {e['start_time']} {e['summary']}")
        if len(events) > 20:
            lines.append(f"...他 {len(events) - 20} 件")
        if truncated_count:
            lines.append(f"\n⚠️ {truncated_count}件は名前が見切れています。登録後に手動で修正してください。")

        line_service.push_message(user_id, "\n".join(lines))

    except Exception as e:
        logger.error(f"カレンダー画像処理エラー: {e}")
        line_service.push_message(user_id, "画像の解析に失敗しました。TimeTreeのカレンダー画面を送ってください。")


def _register_pending_events(user_id: str, reply_token: str):
    """確認済みの予定をGoogleカレンダーに一括登録し、見切れ予定の修正フローを開始"""
    events = state_store.get_state(KEY_CALENDAR, user_id)
    state_store.del_state(KEY_CALENDAR, user_id)
    if not events:
        line_service.reply_message(reply_token, "登録する予定がありません。")
        return

    tz = pytz.timezone(TIMEZONE)
    added, failed = [], []
    truncated_fixes = []

    for e in events:
        try:
            if e.get("all_day"):
                result = calendar_service.create_allday_event(e["summary"], e["date"])
            else:
                start_dt = datetime.strptime(f"{e['date']} {e['start_time']}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
                end_dt = datetime.strptime(f"{e['date']} {e['end_time']}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
                result = calendar_service.create_event(e["summary"], start_dt, end_dt)
            added.append(e)
            if e.get("truncated"):
                truncated_fixes.append({
                    "event_id": result["id"],
                    "original_summary": e["summary"],
                    "date": e["date"],
                    "time_str": "終日" if e.get("all_day") else e.get("start_time", ""),
                })
        except Exception as ex:
            logger.error(f"イベント登録失敗: {e.get('summary')} - {ex}")
            failed.append(e)

    lines = [f"✅ {len(added)}件の予定をGoogleカレンダーに登録しました！"]
    if failed:
        lines.append(f"⚠️ {len(failed)}件の登録に失敗しました")
    line_service.reply_message(reply_token, "\n".join(lines))

    if truncated_fixes:
        state_store.set_state(KEY_TRUNCATED, user_id, truncated_fixes)
        _ask_next_truncated_fix(user_id)


@app.get("/")
async def health_check():
    return {"status": "ok", "message": "LINE Calendar Bot is running"}


@app.post("/cron/morning-report")
async def cron_morning_report(request: Request):
    """Railway Cronから7時に呼び出される朝の通知エンドポイント"""
    auth = request.headers.get("Authorization", "")
    if not CRON_SECRET or auth != f"Bearer {CRON_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")
    send_morning_report()
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()

    try:
        events = parser.parse(body.decode("utf-8"), signature)
    except Exception as e:
        logger.error(f"署名検証エラー: {e}")
        raise HTTPException(status_code=400, detail="Invalid signature")

    for event in events:
        if not isinstance(event, MessageEvent):
            continue

        # 画像メッセージ処理
        if isinstance(event.message, ImageMessageContent):
            line_service.reply_message(
                event.reply_token,
                "📷 画像を受け取りました！\nカレンダーを解析してGoogleカレンダーに登録します...\n（数秒〜十数秒かかります）"
            )
            background_tasks.add_task(_process_calendar_image, event.message.id, event.source.user_id)
            continue

        if not isinstance(event.message, TextMessageContent):
            continue

        user_text = event.message.text.strip()
        reply_token = event.reply_token
        user_id = event.source.user_id

        logger.info(f"受信メッセージ: {user_text}")

        # 見切れ予定の名前修正
        if state_store.has_state(KEY_TRUNCATED, user_id):
            fixes = state_store.get_state(KEY_TRUNCATED, user_id)
            if fixes:
                fix = fixes.pop(0)
                if user_text not in ("スキップ", "skip"):
                    try:
                        calendar_service.update_event_summary(fix["event_id"], user_text)
                        line_service.reply_message(reply_token, f"✅ 「{user_text}」に更新しました")
                    except Exception as ex:
                        logger.error(f"イベント名更新エラー: {ex}")
                        line_service.reply_message(reply_token, "更新に失敗しました。スキップします。")
                else:
                    line_service.reply_message(reply_token, "スキップしました。")
                if fixes:
                    state_store.set_state(KEY_TRUNCATED, user_id, fixes)
                    _ask_next_truncated_fix(user_id)
                else:
                    state_store.del_state(KEY_TRUNCATED, user_id)
                    line_service.push_message(user_id, "✅ 全ての予定の修正が完了しました！")
            continue

        # 画像解析後の確認待ち処理
        if state_store.has_state(KEY_CALENDAR, user_id):
            if user_text in ("はい", "yes", "YES", "登録", "OK", "ok"):
                _register_pending_events(user_id, reply_token)
                continue
            elif user_text in ("いいえ", "no", "NO", "キャンセル", "cancel", "中止", "やめる"):
                state_store.del_state(KEY_CALENDAR, user_id)
                line_service.reply_message(reply_token, "キャンセルしました。")
                continue

        # 保留中の終日イベントを取得（あれば意図判定に渡す）
        user_pending = state_store.get_state(KEY_ALLDAY, user_id) or None

        try:
            parsed = nlp_parser.parse_intent(user_text, pending_allday_events=user_pending)
            intent = parsed.get("intent", "unknown")
            logger.info(f"intent: {intent}")
        except Exception as e:
            logger.error(f"intent解析エラー: {e}")
            line_service.reply_message(reply_token, "メッセージの解析に失敗しました。もう一度送ってみてください。")
            continue

        # 終日予定の時間更新
        if intent == "time_update" and user_pending:
            try:
                tz = pytz.timezone(TIMEZONE)
                target_summary = parsed.get("event_summary", "")
                date_str = parsed.get("date", datetime.now(tz).strftime("%Y-%m-%d"))
                start_time = parsed.get("start_time", "09:00")
                end_time = parsed.get("end_time", "10:00")

                new_start = datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
                new_end = datetime.strptime(f"{date_str} {end_time}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)

                target_event = next(
                    (e for e in user_pending if target_summary in e.get("summary", "")),
                    user_pending[0],
                )

                calendar_service.update_event_time(target_event["id"], new_start, new_end)
                state_store.del_state(KEY_ALLDAY, user_id)

                reply_text = (
                    f"✅ 予定を更新しました！\n"
                    f"📌 {target_event.get('summary', target_summary)}\n"
                    f"🕐 {new_start.strftime('%m/%d(%a) %H:%M')}〜{new_end.strftime('%H:%M')}"
                )
                line_service.reply_message(reply_token, reply_text)
            except Exception as e:
                logger.error(f"時間更新エラー: {e}")
                line_service.reply_message(reply_token, "予定の更新に失敗しました。「予定名 開始時間〜終了時間」の形式で送ってください。")
            continue

        # 空き時間確認
        if intent == "availability_check":
            try:
                tz = pytz.timezone(TIMEZONE)
                target_dt = datetime.fromisoformat(parsed["target_datetime"]).replace(tzinfo=tz)
                duration = int(parsed.get("duration_minutes", 60))
                result, allday_events = calendar_service.check_availability(target_dt, duration)

                if allday_events:
                    state_store.set_state(KEY_ALLDAY, user_id, allday_events)
                else:
                    state_store.del_state(KEY_ALLDAY, user_id)

                line_service.reply_message(reply_token, result)
            except Exception as e:
                logger.error(f"空き確認エラー: {e}")
                line_service.reply_message(reply_token, "空き時間の確認に失敗しました。時間を指定してもう一度送ってください。")
            continue

        # 予定一覧確認
        if intent == "schedule_query":
            try:
                period = parsed.get("period", "today")
                report = calendar_service.build_query_report(period)
                line_service.reply_message(reply_token, report)
            except Exception as e:
                logger.error(f"予定取得エラー: {e}")
                line_service.reply_message(reply_token, "予定の取得に失敗しました。しばらく後で再試行してください。")
            continue

        # 予定登録
        if intent == "event_creation":
            try:
                schedule = nlp_parser.parse_schedule_text(user_text)
                if schedule is None:
                    line_service.reply_message(
                        reply_token,
                        "予定を読み取れませんでした。\n例：「5/20 15時 田中さんとMTG 1時間」のように送ってください。",
                    )
                    continue

                calendar_service.create_event(
                    summary=schedule["summary"],
                    start_dt=schedule["start"],
                    end_dt=schedule["end"],
                )

                start_str = schedule["start"].strftime("%m/%d(%a) %H:%M")
                end_str = schedule["end"].strftime("%H:%M")
                reply_text = (
                    f"✅ 予定を登録しました！\n"
                    f"📌 {schedule['summary']}\n"
                    f"🕐 {start_str}〜{end_str}"
                )
                line_service.reply_message(reply_token, reply_text)
            except Exception as e:
                logger.error(f"予定登録エラー: {e}")
                line_service.reply_message(reply_token, "予定の登録に失敗しました。しばらく後で再試行してください。")
            continue

        # 判定不能
        line_service.reply_message(
            reply_token,
            "うまく読み取れませんでした。\n"
            "・予定登録：「5/20 15時 田中さんとMTG 1時間」\n"
            "・予定確認：「今日の予定は？」\n"
            "・空き確認：「今日の15時は空いてる？」",
        )

    return {"status": "ok"}
