import os
import logging
from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv()

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException, Request
from linebot.v3.webhook import WebhookParser
from linebot.v3.webhooks import MessageEvent, TextMessageContent

import calendar_service
import line_service
import nlp_parser

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
LINE_USER_ID = os.getenv("LINE_USER_ID", "")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tokyo")

parser = WebhookParser(CHANNEL_SECRET)
scheduler = BackgroundScheduler(timezone=pytz.timezone(TIMEZONE))


def send_morning_report():
    """毎朝7時に実行される予定通知"""
    try:
        logger.info("朝の予定通知を送信中...")
        report = calendar_service.build_daily_report()
        line_service.push_message(LINE_USER_ID, report)
        logger.info("朝の予定通知を送信完了")
    except Exception as e:
        logger.error(f"朝の予定通知エラー: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.add_job(send_morning_report, "cron", hour=7, minute=0)
    scheduler.start()
    logger.info("スケジューラー起動: 毎朝7時に予定通知を送信します")
    yield
    scheduler.shutdown()


app = FastAPI(title="LINE Calendar Bot", lifespan=lifespan)


@app.get("/")
async def health_check():
    return {"status": "ok", "message": "LINE Calendar Bot is running"}


@app.post("/webhook")
async def webhook(request: Request):
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
        if not isinstance(event.message, TextMessageContent):
            continue

        user_text = event.message.text.strip()
        reply_token = event.reply_token

        logger.info(f"受信メッセージ: {user_text}")

        # 「今日の予定」「今週」などの確認コマンド
        if any(kw in user_text for kw in ["今日", "今週", "予定確認", "スケジュール"]):
            try:
                report = calendar_service.build_daily_report()
                line_service.reply_message(reply_token, report)
            except Exception as e:
                logger.error(f"予定取得エラー: {e}")
                line_service.reply_message(reply_token, "予定の取得に失敗しました。しばらく後で再試行してください。")
            continue

        # 予定登録
        try:
            parsed = nlp_parser.parse_schedule_text(user_text)
            if parsed is None:
                line_service.reply_message(
                    reply_token,
                    "予定を読み取れませんでした。\n例：「5/20 15時 田中さんとMTG 1時間」のように送ってください。",
                )
                continue

            event_result = calendar_service.create_event(
                summary=parsed["summary"],
                start_dt=parsed["start"],
                end_dt=parsed["end"],
            )

            start_str = parsed["start"].strftime("%m/%d(%a) %H:%M")
            end_str = parsed["end"].strftime("%H:%M")
            reply_text = (
                f"✅ 予定を登録しました！\n"
                f"📌 {parsed['summary']}\n"
                f"🕐 {start_str}〜{end_str}"
            )
            line_service.reply_message(reply_token, reply_text)

        except Exception as e:
            logger.error(f"予定登録エラー: {e}")
            line_service.reply_message(
                reply_token,
                "予定の登録に失敗しました。しばらく後で再試行してください。",
            )

    return {"status": "ok"}
