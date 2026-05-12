import logging
import os
from datetime import datetime, timezone, timedelta
import httpx

BASE_URL = "https://omapi.onlymonster.ai"
logger   = logging.getLogger(__name__)

# Explicit per-phase timeouts: connect fast, read generous but bounded
_TIMEOUT_DEFAULT  = httpx.Timeout(connect=8, read=20, write=8, pool=5)
_TIMEOUT_MESSAGES = httpx.Timeout(connect=8, read=28, write=8, pool=5)


class OnlyMonsterClient:
    def __init__(self):
        self.api_key = os.getenv("ONLYMONSTER_API_KEY")
        self.headers = {"x-om-auth-token": self.api_key}

    def _get(self, path: str, params: dict | None = None,
             timeout: httpx.Timeout = _TIMEOUT_DEFAULT) -> dict | list:
        url = f"{BASE_URL}{path}"
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, headers=self.headers, params=params)
            if not response.is_success:
                logger.error(
                    "OM API GET %s → HTTP %d\nparams: %s\nbody: %s",
                    url, response.status_code, params, response.text,
                )
                response.raise_for_status()
            return response.json()

    def _post(self, path: str, json_body: dict,
              timeout: httpx.Timeout = _TIMEOUT_DEFAULT) -> dict | list:
        url = f"{BASE_URL}{path}"
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, headers=self.headers, json=json_body, timeout=timeout)
            if not response.is_success:
                logger.error(
                    "OM API POST %s → HTTP %d\nbody: %s",
                    url, response.status_code, response.text,
                )
                response.raise_for_status()
            return response.json()

    def send_message(self, account_id: str, fan_id: str, text: str) -> dict:
        """POST /api/v0/accounts/{account_id}/chats/{fan_id}/messages"""
        path = f"/api/v0/accounts/{account_id}/chats/{fan_id}/messages"
        logger.info("send_message: POST %s text=%r", path, text[:100])
        result = self._post(path, {"text": text}, timeout=_TIMEOUT_MESSAGES)
        logger.info("send_message: response=%s", result)
        return result

    def get_accounts(self) -> list[dict]:
        data = self._get("/api/v0/accounts")
        return data.get("accounts", []) if isinstance(data, dict) else data

    def get_fan_ids(self, account_id: str) -> list[str]:
        data = self._get(f"/api/v0/accounts/{account_id}/fans")
        if isinstance(data, dict):
            return [str(fid) for fid in data.get("fan_ids", [])]
        return [str(fid) for fid in data]

    def get_messages(self, account_id: str, fan_id: str, limit: int = 100) -> list[dict]:
        data = self._get(
            f"/api/v0/accounts/{account_id}/chats/{fan_id}/messages",
            params={"limit": limit},
            timeout=_TIMEOUT_MESSAGES,
        )
        return data.get("items", []) if isinstance(data, dict) else data

    def get_members(self, limit: int = 100, offset: int = 0) -> list[dict]:
        data = self._get("/api/v0/members", params={"limit": limit, "offset": offset})
        return data.get("items", []) if isinstance(data, dict) else data

    def get_transactions(self, account_id: str, fan_id: str | None = None,
                         limit: int = 100, cursor: str | None = None,
                         days: int = 730,
                         start_iso: str | None = None,
                         end_iso:   str | None = None) -> dict:
        """
        Returns the raw response dict so callers can extract both items and next cursor.
        Pass start_iso/end_iso to override the default days-based window.
        """
        now = datetime.now(timezone.utc)
        if start_iso and end_iso:
            start, end = start_iso, end_iso
        else:
            start = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            end   = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        params: dict = {"limit": limit, "start": start, "end": end}
        if fan_id:
            params["fan_id"] = fan_id
        if cursor:
            params["cursor"] = cursor

        data = self._get(
            f"/api/v0/platforms/onlyfans/accounts/{account_id}/transactions",
            params=params,
            timeout=_TIMEOUT_MESSAGES,
        )
        return data if isinstance(data, dict) else {"items": data}

    def get_all_transactions_paged(self, account_id: str, days: int = 730,
                                   start_iso: str | None = None,
                                   end_iso:   str | None = None) -> list[dict]:
        """Cursor-paginate through ALL transactions for an account."""
        result: list[dict] = []
        cursor: str | None = None

        while True:
            resp  = self.get_transactions(account_id, limit=100, cursor=cursor,
                                          days=days, start_iso=start_iso, end_iso=end_iso)
            batch = resp.get("items") or resp.get("transactions") or resp.get("data") or []
            if not batch:
                break
            result.extend(batch)

            # Next cursor — try common field names
            cursor = (resp.get("cursor") or resp.get("next_cursor") or
                      resp.get("nextCursor") or resp.get("next") or None)
            if not cursor:
                break

        logger.info("get_all_transactions_paged acc=%s → %d txns total", account_id, len(result))
        return result

    def get_all_messages_paged(self, account_id: str, fan_id: str) -> list[dict]:
        """Paginate through ALL messages for a fan (deep analysis)."""
        result: list[dict] = []
        offset = 0
        while True:
            data = self._get(
                f"/api/v0/accounts/{account_id}/chats/{fan_id}/messages",
                params={"limit": 100, "offset": offset},
                timeout=_TIMEOUT_MESSAGES,
            )
            batch = data.get("items", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
            if not batch:
                break
            result.extend(batch)
            if len(batch) < 100:
                break
            offset += 100
        return result

    def get_metrics(self, account_id: str,
                    start_iso: str | None = None,
                    end_iso:   str | None = None) -> dict:
        """GET /api/v0/accounts/{id}/metrics with optional date range. Returns {} on failure."""
        params: dict = {}
        if start_iso:
            params["start"] = start_iso
        if end_iso:
            params["end"] = end_iso
        try:
            data = self._get(
                f"/api/v0/accounts/{account_id}/metrics",
                params=params or None,
            )
            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning("get_metrics(%s): %s", account_id, e)
            return {}

    def get_users_metrics(self, user_ids: list[int],
                          start_iso: str | None = None,
                          end_iso:   str | None = None) -> list[dict]:
        """
        GET /api/v0/users/metrics?user_ids=<id>&user_ids=<id>&start=...&end=...
        Returns list of per-user metric dicts, or [] on failure.
        """
        # httpx serialises list values as repeated params: user_ids=46038&user_ids=168106
        params: dict = {"user_ids": user_ids, "limit": 100, "offset": 0}
        if start_iso:
            params["from"] = start_iso
        if end_iso:
            params["to"] = end_iso
        try:
            data = self._get("/api/v0/users/metrics", params=params)
            logger.debug("get_users_metrics raw response: %s", data)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return (data.get("items") or data.get("users") or
                        data.get("metrics") or data.get("data") or [])
            return []
        except Exception as e:
            logger.warning("get_users_metrics%s: %s", user_ids, e)
            return []

    def ping(self) -> bool:
        self.get_accounts()
        return True
