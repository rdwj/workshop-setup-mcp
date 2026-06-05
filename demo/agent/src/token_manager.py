"""Keycloak token manager with proactive refresh and 401 retry support."""

from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)


class TokenManager:
    """Acquires and refreshes Keycloak JWTs for MCP gateway auth."""

    def __init__(self):
        self.keycloak_url = os.environ.get("KEYCLOAK_URL", "")
        self.realm = os.environ.get("KEYCLOAK_REALM", "mcp-gateway")
        self.client_id = os.environ.get("KEYCLOAK_CLIENT_ID", "")
        self.client_secret = os.environ.get("KEYCLOAK_CLIENT_SECRET", "")
        self._token: str = ""
        self._expires_at: float = 0
        self._refresh_task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.keycloak_url and self.client_id and self.client_secret)

    @property
    def token(self) -> str:
        return self._token

    @property
    def token_endpoint(self) -> str:
        return f"{self.keycloak_url}/realms/{self.realm}/protocol/openid-connect/token"

    async def acquire(self) -> str:
        if not self.enabled:
            return ""

        async with httpx.AsyncClient(verify=False) as client:
            response = await client.post(
                self.token_endpoint,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                    "scope": "openid groups",
                },
            )
            response.raise_for_status()
            data = response.json()

        self._token = data["access_token"]
        expires_in = data.get("expires_in", 300)
        self._expires_at = time.time() + expires_in
        os.environ["MCP_AUTH_TOKEN"] = self._token

        logger.info(
            "JWT acquired (expires in %ds, refresh in %ds)",
            expires_in,
            max(expires_in - 60, 30),
        )
        return self._token

    async def start_refresh_loop(self) -> None:
        if not self.enabled:
            return
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def _refresh_loop(self) -> None:
        while True:
            remaining = self._expires_at - time.time()
            sleep_for = max(remaining - 60, 30)
            await asyncio.sleep(sleep_for)
            try:
                await self.acquire()
                logger.info("Token refreshed proactively")
            except Exception:
                logger.exception("Token refresh failed, retrying in 30s")
                await asyncio.sleep(30)

    def stop(self) -> None:
        if self._refresh_task:
            self._refresh_task.cancel()
