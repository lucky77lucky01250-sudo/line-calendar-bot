import logging
import os
from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)

logger = logging.getLogger(__name__)

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")


def _get_api() -> MessagingApi:
    configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
    return MessagingApi(ApiClient(configuration))


def push_message(user_id: str, text: str) -> None:
    api = _get_api()
    api.push_message(
        PushMessageRequest(
            to=user_id,
            messages=[TextMessage(type="text", text=text)],
        )
    )


def reply_message(reply_token: str, text: str) -> None:
    api = _get_api()
    api.reply_message(
        ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(type="text", text=text)],
        )
    )


def reply_or_push(reply_token: str, user_id: str, text: str) -> None:
    """reply_messageを試み、トークン期限切れなど失敗時はpush_messageにフォールバック"""
    try:
        reply_message(reply_token, text)
    except Exception as e:
        logger.warning(f"reply_message失敗、push_messageにフォールバック: {e}")
        push_message(user_id, text)
