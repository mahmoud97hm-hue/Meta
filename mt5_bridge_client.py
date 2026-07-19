"""
mt5_bridge_client.py — Async HTTP client for the mt5-bridge REST API.

Provides a singleton client that wraps all bridge endpoints:
  tick polling, trade execution, position management, deals history.
Designed as a 1:1 match to the bridge's REST interface defined at:
  https://github.com/Monkeyattack/mt5-bridge
"""

import asyncio
import time
from datetime import datetime
from typing import Any

import aiohttp

from state import get_http, log_exception, c_log

_BRIDGE_URL: str = ''
_ACCOUNT_ID: str = ''
_client_instance: 'MT5BridgeClient | None' = None


def configure_bridge_client(bridge_url: str, account_id: str) -> None:
    global _BRIDGE_URL, _ACCOUNT_ID
    _BRIDGE_URL = bridge_url.rstrip('/')
    _ACCOUNT_ID = account_id


def get_bridge_client() -> 'MT5BridgeClient':
    global _client_instance
    if _client_instance is None:
        _client_instance = MT5BridgeClient(_BRIDGE_URL, _ACCOUNT_ID)
    return _client_instance


class BridgeHTTPError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f'Bridge HTTP {status}: {detail}')


class BridgeConnectionError(Exception):
    pass


class MT5BridgeClient:
    def __init__(self, base_url: str, account_id: str):
        self._base = base_url
        self._account_id = account_id
        self._last_request_ok = True
        self._consecutive_failures = 0

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        http = get_http()
        url = f'{self._base}{path}'
        try:
            async with http.request(method, url, timeout=aiohttp.ClientTimeout(total=15), **kwargs) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    self._consecutive_failures += 1
                    self._last_request_ok = False
                    raise BridgeHTTPError(resp.status, body[:500])
                self._consecutive_failures = 0
                self._last_request_ok = True
                if resp.status == 204:
                    return None
                return await resp.json()
        except asyncio.TimeoutError:
            self._consecutive_failures += 1
            self._last_request_ok = False
            raise BridgeConnectionError(f'Request timed out: {method} {path}')
        except aiohttp.ClientError as e:
            self._consecutive_failures += 1
            self._last_request_ok = False
            raise BridgeConnectionError(f'{e}')

    @property
    def is_healthy(self) -> bool:
        return self._consecutive_failures < 5

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def reset_failures(self) -> None:
        self._consecutive_failures = 0

    # ── Health ──
    async def health(self) -> dict:
        return await self._request('GET', '/health')

    # ── Account Info ──
    async def get_account_info(self) -> dict:
        return await self._request('GET', f'/accounts/{self._account_id}/info')

    # ── Tick ──
    async def get_tick(self, symbol: str) -> dict:
        return await self._request('GET', f'/accounts/{self._account_id}/tick/{symbol}')

    # ── Positions ──
    async def get_positions(self) -> list[dict]:
        data = await self._request('GET', f'/accounts/{self._account_id}/positions')
        return data.get('positions', [])

    # ── Trade Execution ──
    async def execute_trade(self, symbol: str, action: str, volume: float,
                            stop_loss: float = 0.0, take_profit: float = 0.0,
                            slippage: int = 20, filling_mode: str = 'IOC',
                            comment: str = '') -> dict:
        payload = {
            'symbol': symbol,
            'action': action,
            'volume': volume,
            'stopLoss': stop_loss,
            'takeProfit': take_profit,
            'slippage': slippage,
            'fillingMode': filling_mode,
            'comment': comment,
        }
        return await self._request('POST', f'/accounts/{self._account_id}/trade', json=payload)

    # ── Pending Orders ──
    async def create_pending_order(self, symbol: str, action: str, volume: float,
                                   price: float, stop_loss: float = 0.0,
                                   take_profit: float = 0.0,
                                   order_type: str = 'limit',
                                   expiration: str = None,
                                   comment: str = '') -> dict:
        payload = {
            'symbol': symbol,
            'action': action,
            'volume': volume,
            'price': price,
            'stopLoss': stop_loss,
            'takeProfit': take_profit,
            'orderType': order_type,
            'comment': comment,
        }
        if expiration:
            payload['expiration'] = expiration
        return await self._request('POST', f'/accounts/{self._account_id}/orders/pending', json=payload)

    async def delete_pending_order(self, order_id: int) -> dict:
        return await self._request('DELETE', f'/accounts/{self._account_id}/orders/{order_id}')

    # ── Position Modification ──
    async def modify_position(self, position_id: str, stop_loss: float = None,
                               take_profit: float = None) -> bool:
        payload = {}
        if stop_loss is not None:
            payload['stopLoss'] = stop_loss
        if take_profit is not None:
            payload['takeProfit'] = take_profit
        try:
            await self._request('POST', f'/accounts/{self._account_id}/positions/{position_id}/modify', json=payload)
            return True
        except (BridgeHTTPError, BridgeConnectionError):
            return False

    # ── Position Close ──
    async def close_position(self, position_id: str) -> dict:
        return await self._request('POST', f'/accounts/{self._account_id}/positions/{position_id}/close')

    # ── Deals History ──
    async def get_deals(self, from_time: str, to_time: str) -> list[dict]:
        data = await self._request(
            'GET',
            f'/accounts/{self._account_id}/deals',
            params={'from_time': from_time, 'to_time': to_time},
        )
        return data.get('deals', [])

    # ── Pool Stats ──
    async def get_pool_stats(self) -> dict:
        return await self._request('GET', '/pool/stats')
