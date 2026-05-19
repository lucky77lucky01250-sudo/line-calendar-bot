from __future__ import annotations
import os
from datetime import datetime, timedelta
import pytz
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

TIMEZONE = os.getenv("TIMEZONE", "Asia/Tokyo")
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _get_service():
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN")

    if not all([client_id, client_secret, refresh_token]):
        raise ValueError("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN が設定されていません")

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build("calendar", "v3", credentials=creds)


def _jst_now():
    return datetime.now(pytz.timezone(TIMEZONE))


def get_events(start_dt: datetime, end_dt: datetime, filter_by_start: bool = False) -> list[dict]:
    service = _get_service()
    result = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=start_dt.isoformat(),
        timeMax=end_dt.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    events = result.get("items", [])
    if filter_by_start:
        tz = pytz.timezone(TIMEZONE)
        filtered = []
        for e in events:
            if _is_allday(e):
                # 終日イベントは date フィールドで判定
                event_date = e["start"]["date"]
                if start_dt.strftime("%Y-%m-%d") <= event_date < end_dt.strftime("%Y-%m-%d"):
                    filtered.append(e)
            else:
                event_start = datetime.fromisoformat(e["start"]["dateTime"])
                if start_dt <= event_start < end_dt:
                    filtered.append(e)
        return filtered
    return events


def _is_allday(event: dict) -> bool:
    return "date" in event["start"] and "dateTime" not in event["start"]


def _format_event(event: dict) -> str:
    if _is_allday(event):
        return f"  終日 {event.get('summary', '(タイトルなし)')}（詳細要確認）"
    dt = datetime.fromisoformat(event["start"]["dateTime"])
    time_str = dt.strftime("%H:%M")
    return f"  {time_str} {event.get('summary', '(タイトルなし)')}"


def build_daily_report() -> str:
    """朝の自動通知用：今日・今週の予定"""
    tz = pytz.timezone(TIMEZONE)
    now = _jst_now()
    lines = []

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    today_events = get_events(today_start, today_end, filter_by_start=True)
    lines.append(f"📅 【今日の予定】{now.strftime('%m/%d(%a)')}")
    if today_events:
        lines.extend([_format_event(e) for e in today_events])
    else:
        lines.append("  予定なし")
    lines.append("")

    days_to_sunday = 6 - now.weekday()
    week_end = (now + timedelta(days=days_to_sunday)).replace(
        hour=23, minute=59, second=59, microsecond=0
    )
    tomorrow_start = today_end
    week_events = get_events(tomorrow_start, week_end)
    lines.append(f"📆 【今週の残り予定】〜{week_end.strftime('%m/%d(%a)')}")
    if week_events:
        prev_date = None
        for e in week_events:
            start_val = e["start"].get("dateTime", e["start"].get("date", ""))
            date_str = start_val[:10]
            if date_str != prev_date:
                dt = datetime.fromisoformat(date_str)
                lines.append(f"  {dt.strftime('%m/%d(%a)')}")
                prev_date = date_str
            lines.append(f"    {_format_event(e).strip()}")
    else:
        lines.append("  予定なし")
    lines.append("")

    lines.append("📌 予定を追加するには「日時 タイトル」で送ってください")
    lines.append("例：5/20 15時 田中さんとMTG 1時間")
    return "\n".join(lines)


def build_query_report(period: str) -> str:
    """ユーザーの問い合わせ用：today または week"""
    tz = pytz.timezone(TIMEZONE)
    now = _jst_now()
    lines = []

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)

    if period == "today":
        events = get_events(today_start, today_end, filter_by_start=True)
        lines.append(f"📅 【今日の予定】{now.strftime('%m/%d(%a)')}")
        if events:
            lines.extend([_format_event(e) for e in events])
        else:
            lines.append("  予定なし")
    else:
        days_to_sunday = 6 - now.weekday()
        week_end = (now + timedelta(days=days_to_sunday)).replace(
            hour=23, minute=59, second=59, microsecond=0
        )
        events = get_events(today_start, week_end)
        lines.append(f"📆 【今週の予定】〜{week_end.strftime('%m/%d(%a)')}")
        if events:
            prev_date = None
            for e in events:
                start_val = e["start"].get("dateTime", e["start"].get("date", ""))
                date_str = start_val[:10]
                if date_str != prev_date:
                    dt = datetime.fromisoformat(date_str)
                    lines.append(f"  {dt.strftime('%m/%d(%a)')}")
                    prev_date = date_str
                lines.append(f"    {_format_event(e).strip()}")
        else:
            lines.append("  予定なし")

    return "\n".join(lines)


def check_availability(target_dt: datetime, duration_minutes: int = 60) -> tuple[str, list[dict]]:
    """
    指定時間帯の空き確認。
    戻り値: (返信メッセージ, 終日イベントのリスト)
    終日イベントは後から時間更新できるよう呼び出し元で保持する。
    """
    end_dt = target_dt + timedelta(minutes=duration_minutes)
    events = get_events(target_dt, end_dt)

    time_str = target_dt.strftime("%m/%d(%a) %H:%M")
    end_str = end_dt.strftime("%H:%M")

    if not events:
        return f"✅ {time_str}〜{end_str} は空いています！", []

    allday_events = [e for e in events if _is_allday(e)]
    timed_events = [e for e in events if not _is_allday(e)]

    lines = [f"🗓 {time_str}〜{end_str} の状況："]

    if timed_events:
        lines.append("⛔ 以下の予定が入っています：")
        for e in timed_events:
            dt = datetime.fromisoformat(e["start"]["dateTime"])
            end = datetime.fromisoformat(e["end"]["dateTime"])
            lines.append(f"  {dt.strftime('%H:%M')}〜{end.strftime('%H:%M')} {e.get('summary', '(タイトルなし)')}")

    if allday_events:
        lines.append("⚠️ 終日予定あり（詳細要確認）：")
        for e in allday_events:
            lines.append(f"  📌 {e.get('summary', '(タイトルなし)')}")
        lines.append("")
        lines.append("時間帯を教えてもらえれば更新します。")
        lines.append("例：「〇〇 14時〜16時」")

    return "\n".join(lines), allday_events


def update_event_time(event_id: str, new_start: datetime, new_end: datetime) -> dict:
    """終日イベントを時間指定イベントに更新する"""
    service = _get_service()
    tz = pytz.timezone(TIMEZONE)
    body = {
        "start": {"dateTime": new_start.astimezone(tz).isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": new_end.astimezone(tz).isoformat(), "timeZone": TIMEZONE},
    }
    return service.events().patch(calendarId=CALENDAR_ID, eventId=event_id, body=body).execute()


def create_event(summary: str, start_dt: datetime, end_dt: datetime, description: str = "") -> dict:
    service = _get_service()
    tz = pytz.timezone(TIMEZONE)
    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_dt.astimezone(tz).isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end_dt.astimezone(tz).isoformat(), "timeZone": TIMEZONE},
    }
    return service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
