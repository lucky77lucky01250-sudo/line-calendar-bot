import os
import json
from datetime import datetime, timedelta
import pytz
import anthropic

TIMEZONE = os.getenv("TIMEZONE", "Asia/Tokyo")


def parse_schedule_text(text: str) -> dict | None:
    """
    自然言語テキストから予定情報を抽出する。
    戻り値: {"summary": str, "start": datetime, "end": datetime} or None
    """
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    prompt = f"""以下のテキストから予定情報を抽出してください。
今日の日付: {now.strftime('%Y年%m月%d日(%A)')}
現在時刻: {now.strftime('%H:%M')}

テキスト: {text}

以下のJSON形式で回答してください（余分なテキストは不要）:
{{
  "summary": "予定のタイトル",
  "date": "YYYY-MM-DD",
  "start_time": "HH:MM",
  "duration_minutes": 60
}}

解析できない場合は null を返してください。
日付が「明日」「来週」などの相対表現の場合は今日({now.strftime('%Y-%m-%d')})を基準に計算してください。
時間が不明の場合は "09:00" をデフォルトにしてください。
所要時間が不明の場合は 60 をデフォルトにしてください。"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text.strip()

    if response_text == "null":
        return None

    data = json.loads(response_text)

    start_dt = datetime.strptime(
        f"{data['date']} {data['start_time']}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=tz)
    end_dt = start_dt + timedelta(minutes=int(data["duration_minutes"]))

    return {
        "summary": data["summary"],
        "start": start_dt,
        "end": end_dt,
    }
