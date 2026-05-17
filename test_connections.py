import os
from dotenv import load_dotenv
load_dotenv()

def test_google_calendar():
    print("Google Calendar API テスト中...")
    try:
        import calendar_service
        from datetime import datetime, timedelta
        import pytz
        tz = pytz.timezone("Asia/Tokyo")
        now = datetime.now(tz)
        events = calendar_service.get_events(now, now + timedelta(days=7))
        print(f"  接続成功！今後7日間の予定: {len(events)}件")
        return True
    except Exception as e:
        print(f"  エラー: {e}")
        return False

def test_line():
    print("LINE API テスト中...")
    try:
        from linebot.v3.messaging import ApiClient, Configuration, MessagingApi
        config = Configuration(access_token=os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
        api = MessagingApi(ApiClient(config))
        info = api.get_bot_info()
        print(f"  接続成功！ボット名: {info.display_name}")
        return True
    except Exception as e:
        print(f"  エラー: {e}")
        return False

def test_anthropic():
    print("Anthropic API テスト中...")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": "hi"}]
        )
        print(f"  接続成功！")
        return True
    except Exception as e:
        print(f"  エラー: {e}")
        return False

if __name__ == "__main__":
    results = []
    results.append(test_google_calendar())
    results.append(test_line())
    results.append(test_anthropic())
    print("\n--- 結果 ---")
    labels = ["Google Calendar", "LINE API", "Anthropic API"]
    for label, ok in zip(labels, results):
        print(f"  {label}: {'OK' if ok else 'NG'}")
