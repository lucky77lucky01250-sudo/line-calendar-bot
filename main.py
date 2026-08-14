import os
import logging
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

import pytz
from fastapi import FastAPI, HTTPException, Request
from linebot.v3.webhook import WebhookParser
from linebot.v3.webhooks import MessageEvent, TextMessageContent

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
KEY_DELETE = "delete"
KEY_UPDATE = "event_update"
KEY_MORNING_SENT = "morning_sent"

# 朝の通知の重複防止フラグの保持時間。日付キーと組み合わせるため、
# その日のうちに消えず翌日分と衝突しない長さにしてある。
MORNING_SENT_TTL = 72000  # 20時間


def _build_event_candidate(event: dict) -> dict:
    """Google Calendar イベントから選択肢用dictを生成する"""
    start = event["start"]
    end = event["end"]
    summary = event.get("summary", "(タイトルなし)")
    if "dateTime" in start:
        s = datetime.fromisoformat(start["dateTime"])
        e = datetime.fromisoformat(end["dateTime"])
        display = f"{s.strftime('%m/%d(%a) %H:%M')}〜{e.strftime('%H:%M')}"
    else:
        display = f"{start['date']}（終日）"
    return {
        "id": event["id"],
        "summary": summary,
        "display": display,
    }


def _reply_candidate_list(reply_token: str, user_id: str, candidates: list[dict], action: str) -> None:
    verb = "削除" if action == "delete" else "変更"
    lines = [f"複数の候補が見つかりました。{verb}する予定の番号を送ってください："]
    for i, c in enumerate(candidates, 1):
        lines.append(f"{i}. {c['summary']}（{c['display']}）")
    line_service.reply_or_push(reply_token, user_id, "\n".join(lines))


def send_morning_report() -> bool:
    """朝の予定通知（今日・今週）。送信できたかどうかを返す。"""
    try:
        logger.info("朝の予定通知を送信中...")
        report = calendar_service.build_daily_report()
        line_service.push_message(LINE_USER_ID, report)
        logger.info("朝の予定通知を送信完了")
        return True
    except Exception as e:
        logger.error(f"朝の予定通知エラー: {e}")
        return False


@app.get("/")
async def health_check():
    return {"status": "ok", "message": "LINE Calendar Bot is running"}


@app.post("/cron/morning-report")
async def cron_morning_report(request: Request):
    """朝の通知エンドポイント。

    GitHub Actions の schedule は最大で数時間ずれるため、外部から1日に複数回
    叩かれる前提にしてある。最初に到達したものだけが送信し、以降はスキップする。
    送信に失敗した場合はフラグを戻して、後続のトリガーで再試行できるようにする。
    """
    auth = request.headers.get("Authorization", "")
    if not CRON_SECRET or auth != f"Bearer {CRON_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")

    today = datetime.now(pytz.timezone(TIMEZONE)).strftime("%Y-%m-%d")
    if not state_store.claim_once(KEY_MORNING_SENT, today, MORNING_SENT_TTL):
        logger.info(f"朝の通知は送信済みのためスキップ: {today}")
        return {"status": "skipped", "date": today}

    if not send_morning_report():
        state_store.del_state(KEY_MORNING_SENT, today)
        raise HTTPException(status_code=500, detail="朝の通知の送信に失敗しました")

    return {"status": "ok", "date": today}


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
        # 確認応答（はい/キャンセル等）の判定には末尾の記号を除いた answer を使う。
        # イベント名の入力にはそのままの user_text を使う（名前に含まれる記号を消さないため）
        answer = user_text.rstrip("！!。．.、，,？?〜～ 　")
        reply_token = event.reply_token
        user_id = event.source.user_id

        logger.info(f"受信メッセージ: {user_text}")

        # 削除確認待ち
        if state_store.has_state(KEY_DELETE, user_id):
            candidates = state_store.get_state(KEY_DELETE, user_id)
            if len(candidates) == 1:
                if answer in ("はい", "yes", "YES", "削除", "OK", "ok"):
                    try:
                        calendar_service.delete_event(candidates[0]["id"])
                        state_store.del_state(KEY_DELETE, user_id)
                        line_service.reply_or_push(reply_token, user_id, f"🗑️ 「{candidates[0]['summary']}」を削除しました。")
                    except Exception as ex:
                        logger.error(f"削除エラー: {ex}")
                        state_store.del_state(KEY_DELETE, user_id)
                        line_service.reply_or_push(reply_token, user_id, "削除に失敗しました。")
                elif answer in ("いいえ", "no", "NO", "キャンセル", "cancel", "中止", "やめる"):
                    state_store.del_state(KEY_DELETE, user_id)
                    line_service.reply_or_push(reply_token, user_id, "キャンセルしました。")
                else:
                    c = candidates[0]
                    line_service.reply_or_push(reply_token, user_id,
                        f"「{c['summary']}（{c['display']}）」を削除しますか？\n「はい」または「キャンセル」で返答してください。")
                continue
            else:
                if user_text.isdigit() and 1 <= int(user_text) <= len(candidates):
                    selected = [candidates[int(user_text) - 1]]
                    state_store.set_state(KEY_DELETE, user_id, selected)
                    c = selected[0]
                    line_service.reply_or_push(reply_token, user_id,
                        f"「{c['summary']}（{c['display']}）」を削除しますか？\n「はい」または「キャンセル」で返答してください。")
                elif answer in ("キャンセル", "cancel", "中止", "やめる"):
                    state_store.del_state(KEY_DELETE, user_id)
                    line_service.reply_or_push(reply_token, user_id, "キャンセルしました。")
                else:
                    _reply_candidate_list(reply_token, user_id, candidates, "delete")
                continue

        # 変更確認待ち
        if state_store.has_state(KEY_UPDATE, user_id):
            update_state = state_store.get_state(KEY_UPDATE, user_id)
            candidates = update_state["candidates"]
            params = update_state["params"]
            if len(candidates) == 1:
                if answer in ("はい", "yes", "YES", "変更", "OK", "ok"):
                    try:
                        result = calendar_service.update_event_datetime(
                            candidates[0]["id"],
                            new_date=params.get("date") or None,
                            new_start_time=params.get("start_time") or None,
                            new_end_time=params.get("end_time") or None,
                        )
                        state_store.del_state(KEY_UPDATE, user_id)
                        start_raw = result["start"].get("dateTime", result["start"].get("date", ""))
                        end_raw = result["end"].get("dateTime", result["end"].get("date", ""))
                        if "T" in start_raw:
                            s = datetime.fromisoformat(start_raw)
                            e = datetime.fromisoformat(end_raw)
                            time_disp = f"{s.strftime('%m/%d(%a) %H:%M')}〜{e.strftime('%H:%M')}"
                        else:
                            time_disp = f"{start_raw}（終日）"
                        line_service.reply_or_push(reply_token, user_id,
                            f"✅ 予定を変更しました！\n📌 {result.get('summary', candidates[0]['summary'])}\n🕐 {time_disp}")
                    except Exception as ex:
                        logger.error(f"変更エラー: {ex}")
                        state_store.del_state(KEY_UPDATE, user_id)
                        line_service.reply_or_push(reply_token, user_id, "変更に失敗しました。")
                elif answer in ("いいえ", "no", "NO", "キャンセル", "cancel", "中止", "やめる"):
                    state_store.del_state(KEY_UPDATE, user_id)
                    line_service.reply_or_push(reply_token, user_id, "キャンセルしました。")
                else:
                    # 時間の追加情報として解析を試みる（例：「22時から1時間」「22時〜23時」）
                    try:
                        tz = pytz.timezone(TIMEZONE)
                        base_date = params.get("date") or datetime.now(tz).strftime("%Y-%m-%d")
                        schedule = nlp_parser.parse_schedule_text(f"{base_date} {user_text}")
                        if schedule:
                            new_params = dict(params)
                            new_params["start_time"] = schedule["start"].strftime("%H:%M")
                            new_params["end_time"] = schedule["end"].strftime("%H:%M")
                            state_store.set_state(KEY_UPDATE, user_id, {"candidates": candidates, "params": new_params})
                            c = candidates[0]
                            date_disp = new_params.get("date", "")
                            line_service.reply_or_push(reply_token, user_id,
                                f"「{c['summary']}」を {date_disp} {new_params['start_time']}〜{new_params['end_time']} に変更しますか？\n「はい」または「キャンセル」で返答してください。")
                        else:
                            c = candidates[0]
                            line_service.reply_or_push(reply_token, user_id,
                                f"「{c['summary']}（{c['display']}）」を変更しますか？\n「はい」または「キャンセル」で返答してください。")
                    except Exception:
                        c = candidates[0]
                        line_service.reply_or_push(reply_token, user_id,
                            f"「{c['summary']}（{c['display']}）」を変更しますか？\n「はい」または「キャンセル」で返答してください。")
                continue
            else:
                if user_text.isdigit() and 1 <= int(user_text) <= len(candidates):
                    selected = [candidates[int(user_text) - 1]]
                    state_store.set_state(KEY_UPDATE, user_id, {"candidates": selected, "params": params})
                    c = selected[0]
                    line_service.reply_or_push(reply_token, user_id,
                        f"「{c['summary']}（{c['display']}）」を変更しますか？\n「はい」または「キャンセル」で返答してください。")
                elif answer in ("キャンセル", "cancel", "中止", "やめる"):
                    state_store.del_state(KEY_UPDATE, user_id)
                    line_service.reply_or_push(reply_token, user_id, "キャンセルしました。")
                else:
                    _reply_candidate_list(reply_token, user_id, candidates, "update")
                continue

        # 確認待ちが無いのに「はい」等の確認応答が来た場合（期限切れなど）は案内を返す
        if answer in ("はい", "yes", "YES", "登録", "OK", "ok",
                      "いいえ", "no", "NO", "キャンセル", "cancel", "中止", "やめる"):
            line_service.reply_or_push(reply_token, user_id,
                "確認待ちの予定が見つかりません。\n（確認待ちは2時間で期限切れになります）\nお手数ですが、もう一度画像を送ってください。")
            continue

        # 保留中の終日イベントを取得（あれば意図判定に渡す）
        user_pending = state_store.get_state(KEY_ALLDAY, user_id) or None

        try:
            parsed = nlp_parser.parse_intent(user_text, pending_allday_events=user_pending)
            intent = parsed.get("intent", "unknown")
            logger.info(f"intent: {intent}")
        except Exception as e:
            logger.error(f"intent解析エラー: {e}")
            line_service.reply_or_push(reply_token, user_id, "メッセージの解析に失敗しました。もう一度送ってみてください。")
            continue

        # 終日予定の時間更新
        if intent == "time_update" and user_pending:
            try:
                tz = pytz.timezone(TIMEZONE)
                target_summary = parsed.get("event_summary", "")
                date_str = parsed.get("date", datetime.now(tz).strftime("%Y-%m-%d"))
                start_time = parsed.get("start_time", "09:00")
                end_time = parsed.get("end_time", "10:00")

                new_start = tz.localize(datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M"))
                new_end = tz.localize(datetime.strptime(f"{date_str} {end_time}", "%Y-%m-%d %H:%M"))

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
                line_service.reply_or_push(reply_token, user_id, reply_text)
            except Exception as e:
                logger.error(f"時間更新エラー: {e}")
                line_service.reply_or_push(reply_token, user_id, "予定の更新に失敗しました。「予定名 開始時間〜終了時間」の形式で送ってください。")
            continue

        # 空き時間確認
        if intent == "availability_check":
            try:
                tz = pytz.timezone(TIMEZONE)
                target_dt = tz.localize(datetime.fromisoformat(parsed["target_datetime"]).replace(tzinfo=None))
                duration = int(parsed.get("duration_minutes", 60))
                result, allday_events = calendar_service.check_availability(target_dt, duration)

                if allday_events:
                    state_store.set_state(KEY_ALLDAY, user_id, allday_events)
                else:
                    state_store.del_state(KEY_ALLDAY, user_id)

                line_service.reply_or_push(reply_token, user_id, result)
            except Exception as e:
                logger.error(f"空き確認エラー: {e}")
                line_service.reply_or_push(reply_token, user_id, "空き時間の確認に失敗しました。時間を指定してもう一度送ってください。")
            continue

        # 予定一覧確認
        if intent == "schedule_query":
            try:
                period = parsed.get("period", "today")
                report = calendar_service.build_query_report(period)
                line_service.reply_or_push(reply_token, user_id, report)
            except Exception as e:
                logger.error(f"予定取得エラー: {e}")
                line_service.reply_or_push(reply_token, user_id, "予定の取得に失敗しました。しばらく後で再試行してください。")
            continue

        # 予定登録
        if intent == "event_creation":
            try:
                schedule = nlp_parser.parse_schedule_text(user_text)
                if schedule is None:
                    line_service.reply_or_push(
                        reply_token,
                        user_id,
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
                line_service.reply_or_push(reply_token, user_id, reply_text)
            except Exception as e:
                logger.error(f"予定登録エラー: {e}")
                line_service.reply_or_push(reply_token, user_id, "予定の登録に失敗しました。しばらく後で再試行してください。")
            continue

        # 予定削除
        if intent == "event_deletion":
            try:
                keyword = parsed.get("summary", "")
                date_str = parsed.get("date", "") or None
                if not keyword:
                    line_service.reply_or_push(reply_token, user_id, "削除したい予定名を教えてください。")
                    continue
                raw_events = calendar_service.search_events_by_keyword(keyword, date_str)
                if not raw_events:
                    line_service.reply_or_push(reply_token, user_id, f"「{keyword}」に一致する予定が見つかりませんでした。")
                    continue
                candidates = [_build_event_candidate(e) for e in raw_events]
                state_store.set_state(KEY_DELETE, user_id, candidates)
                if len(candidates) == 1:
                    c = candidates[0]
                    line_service.reply_or_push(reply_token, user_id,
                        f"「{c['summary']}（{c['display']}）」を削除しますか？\n「はい」または「キャンセル」で返答してください。")
                else:
                    _reply_candidate_list(reply_token, user_id, candidates, "delete")
            except Exception as e:
                logger.error(f"削除処理エラー: {e}")
                line_service.reply_or_push(reply_token, user_id, "削除処理に失敗しました。しばらく後で再試行してください。")
            continue

        # 予定変更
        if intent == "event_update":
            try:
                keyword = parsed.get("summary", "")
                params = {
                    "date": parsed.get("date", ""),
                    "start_time": parsed.get("start_time", ""),
                    "end_time": parsed.get("end_time", ""),
                }
                if not keyword:
                    line_service.reply_or_push(reply_token, user_id, "変更したい予定名を教えてください。")
                    continue
                if not any(params.values()):
                    line_service.reply_or_push(reply_token, user_id,
                        "変更後の日時を教えてください。\n例：「MTGを明日15時に変更して」")
                    continue
                # 変更の場合、dateは新しい日付なので検索には使わない（現在の日付でイベントを探す）
                raw_events = calendar_service.search_events_by_keyword(keyword, None)
                if not raw_events:
                    line_service.reply_or_push(reply_token, user_id, f"「{keyword}」に一致する予定が見つかりませんでした。")
                    continue
                candidates = [_build_event_candidate(e) for e in raw_events]
                state_store.set_state(KEY_UPDATE, user_id, {"candidates": candidates, "params": params})
                if len(candidates) == 1:
                    c = candidates[0]
                    line_service.reply_or_push(reply_token, user_id,
                        f"「{c['summary']}（{c['display']}）」を変更しますか？\n「はい」または「キャンセル」で返答してください。")
                else:
                    _reply_candidate_list(reply_token, user_id, candidates, "update")
            except Exception as e:
                logger.error(f"変更処理エラー: {e}")
                line_service.reply_or_push(reply_token, user_id, "変更処理に失敗しました。しばらく後で再試行してください。")
            continue

        # 判定不能
        line_service.reply_or_push(
            reply_token,
            user_id,
            "うまく読み取れませんでした。\n"
            "・予定登録：「5/20 15時 田中さんとMTG 1時間」\n"
            "・予定確認：「今日の予定は？」\n"
            "・空き確認：「今日の15時は空いてる？」\n"
            "・予定削除：「〇〇を削除して」\n"
            "・予定変更：「〇〇を明日15時に変更して」",
        )

    return {"status": "ok"}
