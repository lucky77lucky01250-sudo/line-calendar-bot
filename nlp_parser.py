from __future__ import annotations
import base64
import os
import json
import re
from datetime import datetime, timedelta
import pytz
import anthropic

TIMEZONE = os.getenv("TIMEZONE", "Asia/Tokyo")


def parse_intent(text: str, pending_allday_events: list[dict] | None = None) -> dict:
    """
    ユーザーメッセージの意図を分類し、必要なパラメータを抽出する。
    intent:
      - "availability_check" : 空き時間の確認
      - "schedule_query"     : 予定一覧の確認
      - "event_creation"     : 予定の登録
      - "time_update"        : 終日予定の時間更新
      - "unknown"            : 判定不能
    """
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # 終日イベントが保留中の場合、コンテキストをプロンプトに追加
    pending_context = ""
    if pending_allday_events:
        names = [e.get("summary", "(タイトルなし)") for e in pending_allday_events]
        pending_context = f"""
【重要】直前のメッセージで以下の終日予定が確認できなかった状態です：
{chr(10).join(f'- {n}' for n in names)}
ユーザーがこれらの予定の時間帯を教えようとしている場合は time_update を返してください。

【時間更新の場合】
{{
  "intent": "time_update",
  "event_summary": "更新対象の予定名（上記リストの中から最も近いもの）",
  "date": "YYYY-MM-DD（終日予定の日付、不明なら今日）",
  "start_time": "HH:MM",
  "end_time": "HH:MM"
}}
"""

    prompt = f"""以下のメッセージを分析して、ユーザーの意図を判定してください。
今日の日付: {now.strftime('%Y年%m月%d日(%A)')}
現在時刻: {now.strftime('%H:%M')}
{pending_context}
メッセージ: {text}

以下のいずれかのJSON形式で回答してください（余分なテキストは不要）:

【空き時間確認の場合】
{{
  "intent": "availability_check",
  "target_datetime": "YYYY-MM-DDTHH:MM:00",
  "duration_minutes": 60
}}

【予定一覧確認の場合】
{{
  "intent": "schedule_query",
  "period": "today" または "week"
}}

【予定登録の場合】
{{
  "intent": "event_creation",
  "summary": "予定のタイトル",
  "date": "YYYY-MM-DD",
  "start_time": "HH:MM",
  "duration_minutes": 60
}}

【判定不能の場合】
{{
  "intent": "unknown"
}}

判定基準:
- 「空いてる？」「空き時間は？」「〇時は？」→ availability_check
- 「今日の予定」「今週は？」「スケジュール」→ schedule_query
- 日時＋タイトルを含む → event_creation
- 相対日付（明日・来週など）は今日({now.strftime('%Y-%m-%d')})を基準に絶対日付へ変換
- duration_minutesが不明な場合は60
- target_datetimeの時間が不明な場合はそのまま記載せずperiodで返す"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text.strip()
    response_text = re.sub(r'^```(?:json)?\s*', '', response_text)
    response_text = re.sub(r'\s*```$', '', response_text).strip()
    return json.loads(response_text)


def parse_schedule_text(text: str) -> dict | None:
    """後方互換用: event_creationの場合のみ呼び出す"""
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
  "end_time": "HH:MM"
}}

ルール:
- 解析できない場合は null を返す
- 日付が「明日」「来週」など相対表現の場合は今日({now.strftime('%Y-%m-%d')})を基準に計算
- 「19時から22時まで」のように終了時刻が明示されている場合はそのまま使う
- 「1時間」「30分」のように所要時間が書かれている場合は start_time + 所要時間 = end_time として計算
- 終了時刻も所要時間も不明な場合は end_time = start_time + 1時間
- 開始時刻が不明な場合は "09:00" をデフォルトにする"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text.strip()
    response_text = re.sub(r'^```(?:json)?\s*', '', response_text)
    response_text = re.sub(r'\s*```$', '', response_text).strip()

    if response_text == "null":
        return None

    data = json.loads(response_text)

    start_dt = tz.localize(datetime.strptime(f"{data['date']} {data['start_time']}", "%Y-%m-%d %H:%M"))
    end_dt = tz.localize(datetime.strptime(f"{data['date']} {data['end_time']}", "%Y-%m-%d %H:%M"))
    if end_dt <= start_dt:
        end_dt = start_dt + timedelta(hours=1)

    return {
        "summary": data["summary"],
        "start": start_dt,
        "end": end_dt,
    }


def parse_calendar_image(image_data: bytes, media_type: str = "image/jpeg") -> list[dict]:
    """
    カレンダー画像から今日以降の予定を抽出する。
    戻り値: [{"summary": str, "date": "YYYY-MM-DD", "start_time": str, "end_time": str, "all_day": bool}]
    """
    tz = pytz.timezone(TIMEZONE)
    today = datetime.now(tz).strftime("%Y-%m-%d")
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    image_b64 = base64.b64encode(image_data).decode("utf-8")

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": image_b64}
                },
                {
                    "type": "text",
                    "text": f"""このTimeTreeカレンダー画像を注意深く解析し、{today}以降の予定のみ抽出してください。
今日の日付: {today}

JSON配列のみを返してください（コードブロック不要、余分なテキスト不要）:
[
  {{"summary": "予定名", "date": "YYYY-MM-DD", "end_date": "", "start_time": "HH:MM", "end_time": "HH:MM", "all_day": false, "truncated": false}},
  {{"summary": "終日予定名…", "date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", "start_time": "", "end_time": "", "all_day": true, "truncated": true}}
]

厳守するルール:
- 画像から明確に読み取れる文字のみ使う（不明瞭・見切れた文字は推測しない）
- テキストが途中で切れている場合はsummaryの末尾に「…」を付け、truncated: true にする
- {today}より前の日付の予定は絶対に含めない
- 日付は画像に表示されている日付を正確に読む（年は{today[:4]}年として扱う）
- 終日予定・複数日イベントはall_day: true、start_time・end_timeは空文字
- 複数日にわたる終日予定はend_dateに最終日（inclusive）を入れ、1日のみの場合はend_dateを空文字にする
- 時刻付き予定の終了時刻が不明な場合は開始の1時間後、end_dateは空文字
- 祝日・六曜（先勝・友引・先負・仏滅・大安・赤口）・曜日表記は除外
- 予定が存在しない場合は空配列 [] を返す"""
                }
            ]
        }]
    )

    response_text = message.content[0].text.strip()
    response_text = re.sub(r'^```(?:json)?\s*', '', response_text)
    response_text = re.sub(r'\s*```$', '', response_text).strip()
    return json.loads(response_text)
