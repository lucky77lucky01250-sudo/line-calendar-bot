from __future__ import annotations
import json
import os
import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
TTL = 7200  # 2時間

_client: redis.Redis | None = None


def _get_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(REDIS_URL, decode_responses=True)
    return _client


def get_state(key: str, user_id: str) -> list:
    data = _get_client().get(f"{key}:{user_id}")
    return json.loads(data) if data else []


def set_state(key: str, user_id: str, value: list, ttl: int = TTL) -> None:
    _get_client().setex(f"{key}:{user_id}", ttl, json.dumps(value, ensure_ascii=False))


def del_state(key: str, user_id: str) -> None:
    _get_client().delete(f"{key}:{user_id}")


def has_state(key: str, user_id: str) -> bool:
    return _get_client().exists(f"{key}:{user_id}") > 0


def claim_once(key: str, member: str, ttl: int) -> bool:
    """同じ key:member の組で最初の1回だけ True を返す（重複実行の防止）。
    SET NX による原子的な処理なので、複数のトリガーがほぼ同時に来ても
    通過するのは1つだけになる。"""
    return bool(_get_client().set(f"{key}:{member}", "1", nx=True, ex=ttl))
