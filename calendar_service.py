import os
import json
from datetime import datetime, timedelta
import pytz
from google.oauth2 import service_account
from googleapiclient.discovery import build

TIMEZONE = os.getenv("TIMEZONE", "Asia/Tokyo")
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _get_service():
    credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not credentials_json:
        raise ValueError("GOOGLE_CREDENTIALS_JSON が設定されていません")
    credentials_info = json.loads(credentials_json)
    credentials = service_account.Credentials.from_service_account_info(
        credentials_info, scopes=SCOPES
    )
    return build("calendar", "v3", credentials=credentials)


def _jst_now():
    return datetime.now(pytz.timezone(TIMEZONE))


def get_events(start_dt: datetime, end_dt: datetime) -> list[dict]:
    service = _get_service()
    result = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=start_dt.isoformat(),
        timeMax=end_dt.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    return result.get("items", [])


def _format_event(event: dict) -> str:
    start = event["start"].get("dateTime", event["start"].get("date", ""))
    if "T" in start:
        dt = datetime.fromisoformat(start)
        time_str = dt.strftime("%H:%M")
    else:
        time_str = "終日"
    return f"  {time_str} {event.get('summary', '(タイトルなし)')}"


def _week_range(weeks_ahead: int, now: datetime) -> tuple[datetime, datetime]:
    """weeks_ahead週後の月曜〜日曜を返す"""
    tz = pytz.timezone(TIMEZONE)
    days_to_monday = now.weekday()
    this_monday = now - timedelta(days=days_to_monday)
    start = (this_monday + timedelta(weeks=weeks_ahead)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end = start + timedelta(days=7)
    return start.astimezone(tz), end.astimezone(tz)


def build_daily_report() -> str:
    tz = pytz.timezone(TIMEZONE)
    now = _jst_now()
    lines = []

    # 今日の予定
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    today_events = get_events(today_start, today_end)
    lines.append(f"📅 【今日の予定】{now.strftime('%m/%d(%a)')}")
    if today_events:
        lines.extend([_format_event(e) for e in today_events])
    else:
        lines.append("  予定なし")
    lines.append("")

    # 今週の予定（今日以降〜日曜）
    week_end_start, week_end_end = _week_range(0, now)
    week_events = get_events(now, week_end_end)
    lines.append(f"📆 【今週の残り予定】〜{week_end_end.strftime('%m/%d')}")
    if week_events:
        prev_date = None
        for e in week_events:
            start = e["start"].get("dateTime", e["start"].get("date", ""))
            date_str = start[:10]
            if date_str != prev_date:
                dt = datetime.fromisoformat(date_str)
                lines.append(f"  {dt.strftime('%m/%d(%a)')}")
                prev_date = date_str
            lines.append(f"    {_format_event(e).strip()}")
    else:
        lines.append("  予定なし")
    lines.append("")

    # 2〜4週間後の予定
    labels = ["2週間後", "3週間後", "4週間後"]
    emojis = ["🗓", "🗓", "🗓"]
    for i, (label, emoji) in enumerate(zip(labels, emojis), start=2):
        wk_start, wk_end = _week_range(i, now)
        events = get_events(wk_start, wk_end)
        lines.append(
            f"{emoji} 【{label}の予定】{wk_start.strftime('%m/%d')}〜{wk_end.strftime('%m/%d')}"
        )
        if events:
            prev_date = None
            for e in events:
                start = e["start"].get("dateTime", e["start"].get("date", ""))
                date_str = start[:10]
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
