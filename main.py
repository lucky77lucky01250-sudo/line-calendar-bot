import os
import logging
from datetime import datetime, timedelta
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
KEY_DELETE = "delete"
KEY_UPDATE = "event_update"


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

        # 重複チェックして各イベントにフラグを付ける
        duplicate_indices = calendar_service.find_duplicates(events)
        for i, e in enumerate(events):
            e["duplicate"] = i in duplicate_indices

        state_store.set_state(KEY_CALENDAR, user_id, events)

        new_count = sum(1 for e in events if not e.get("duplicate"))
        dup_count = len(duplicate_indices)
        truncated_count = sum(1 for e in events if e.get("truncated") and not e.get("duplicate"))

        lines = [f"📅 {len(events)}件の予定を検出しました。（✅新規 / 🔄重複スキップ / ⚠️名前見切れ）\n「はい」で登録、「キャンセル」で中止してください。\n"]
        for e in events[:20]:
            if e.get("duplicate"):
                prefix = "🔄"
            elif e.get("truncated"):
                prefix = "⚠️"
            else:
                prefix = "✅"
            if e.get("all_day"):
                line = f"{prefix} {e['date']} {e['summary']}（終日）"
            else:
                line = f"{prefix} {e['date']} {e['start_time']} {e['summary']}"
            if e.get("duplicate"):
                line += " ← 重複・スキップ"
            lines.append(line)
        if len(events) > 20:
            lines.append(f"...他 {len(events) - 20} 件")
        if truncated_count:
            lines.append(f"\n⚠️ {truncated_count}件は名前が見切れています。登録後に手動で修正してください。")
        if new_count == 0:
            lines.append("\n※ 新規登録する予定はありません。")

        line_service.push_message(user_id, "\n".join(lines))

    except Exception as e:
        logger.error(f"カレンダー画像処理エラー: {e}")
        line_service.push_message(user_id, "画像の解析に失敗しました。TimeTreeのカレンダー画面を送ってください。")


def _register_pending_events(user_id: str, reply_token: str):
    """確認済みの予定をGoogleカレンダーに一括登録し、見切れ予定の修正フローを開始"""
    events = state_store.get_state(KEY_CALENDAR, user_id)
    state_store.del_state(KEY_CALENDAR, user_id)
    if not events:
        line_service.reply_or_push(reply_token, user_id, "登録する予定がありません。")
        return

    # 重複としてマークされた予定はスキップ
    target_events = [e for e in events if not e.get("duplicate")]
    dup_count = len(events) - len(target_events)

    if not target_events:
        line_service.reply_or_push(reply_token, user_id,
            f"🔄 全{len(events)}件が既にカレンダーに存在するためスキップしました。")
        return

    tz = pytz.timezone(TIMEZONE)
    added, failed = [], []
    truncated_fixes = []

    for e in target_events:
        try:
            if e.get("all_day"):
                raw_end = e.get("end_date", "")
                # end_date は inclusive 最終日なので +1日して exclusive に変換
                if raw_end:
                    exc_end = (datetime.strptime(raw_end, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
                else:
                    exc_end = None
                result = calendar_service.create_allday_event(e["summary"], e["date"], exc_end)
            else:
                start_dt = tz.localize(datetime.strptime(f"{e['date']} {e['start_time']}", "%Y-%m-%d %H:%M"))
                end_dt = tz.localize(datetime.strptime(f"{e['date']} {e['end_time']}", "%Y-%m-%d %H:%M"))
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
    if dup_count:
        lines.append(f"🔄 {dup_count}件は既存と重複のためスキップしました")
    if failed:
        lines.append(f"⚠️ {len(failed)}件の登録に失敗しました")
    line_service.reply_or_push(reply_token, user_id, "\n".join(lines))

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
            line_service.reply_or_push(
                event.reply_token,
                event.source.user_id,
                "📷 画像を受け取りました！\nカレンダーを解析してGoogleカレンダーに登録します...\n（数秒〜十数秒かかります）"
            )
            background_tasks.add_task(_process_calendar_image, event.message.id, event.source.user_id)
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

        # 見切れ予定の名前修正
        if state_store.has_state(KEY_TRUNCATED, user_id):
            fixes = state_store.get_state(KEY_TRUNCATED, user_id)
            if fixes:
                fix = fixes.pop(0)
                if answer not in ("スキップ", "skip"):
                    try:
                        calendar_service.update_event_summary(fix["event_id"], user_text)
                        line_service.reply_or_push(reply_token, user_id, f"✅ 「{user_text}」に更新しました")
                    except Exception as ex:
                        logger.error(f"イベント名更新エラー: {ex}")
                        line_service.reply_or_push(reply_token, user_id, "更新に失敗しました。スキップします。")
                else:
                    line_service.reply_or_push(reply_token, user_id, "スキップしました。")
                if fixes:
                    state_store.set_state(KEY_TRUNCATED, user_id, fixes)
                    _ask_next_truncated_fix(user_id)
                else:
                    state_store.del_state(KEY_TRUNCATED, user_id)
                    line_service.push_message(user_id, "✅ 全ての予定の修正が完了しました！")
            continue

        # 画像解析後の確認待ち処理
        if state_store.has_state(KEY_CALENDAR, user_id):
            if answer in ("はい", "yes", "YES", "登録", "OK", "ok"):
                _register_pending_events(user_id, reply_token)
                continue
            elif answer in ("いいえ", "no", "NO", "キャンセル", "cancel", "中止", "やめる"):
                state_store.del_state(KEY_CALENDAR, user_id)
                line_service.reply_or_push(reply_token, user_id, "キャンセルしました。")
                continue
            else:
                line_service.reply_or_push(reply_token, user_id,
                    "「はい」で登録、「キャンセル」で中止してください。")
                continue

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
