"""
Pytest Global Configuration & Mock Setup

This file defines a global autouse fixture that detects the presence of local Kalshi 
credentials and PEM files. If they are missing, it dynamically injects mock classes 
to intercept REST requests, database connection calls, and websocket connections. 
This enables the test suite to execute successfully and reliably in isolated CI environments.
"""

import pytest
import os
import json
import asyncio
from unittest.mock import patch, MagicMock

# Define Mock HTTP Responses
class MockResponse:
    def __init__(self, json_data, status_code):
        self.json_data = json_data
        self.status_code = status_code
        self.text = json.dumps(json_data)

    def json(self):
        return self.json_data

def mock_request_handler(method, url, *args, **kwargs):
    if "markets" in url:
        return MockResponse({
            "markets": [{"ticker": "MOCK_TICKER", "status": "active", "close_time": "2030-01-01T00:00:00Z"}],
            "market": {"ticker": "MOCK_TICKER", "status": "active", "close_time": "2030-01-01T00:00:00Z"}
        }, 200)
    elif "portfolio/balance" in url:
        return MockResponse({"balance": 10000}, 200)
    elif "portfolio/positions" in url:
        return MockResponse({"market_positions": []}, 200)
    elif "portfolio/orders" in url or "portfolio/events/orders" in url:
        if method == "POST":
            return MockResponse({"order": {"order_id": "mock-order-id-123", "status": "executed"}}, 201)
        elif method == "DELETE":
            return MockResponse({"order": {"order_id": "mock-order-id-123", "status": "canceled"}}, 200)
    return MockResponse({}, 200)

# Define Mock Database cursor and connection supporting context manager protocols
class MockCursor:
    def execute(self, *args, **kwargs):
        pass
    def fetchone(self):
        return None
    def fetchall(self):
        return []
    def close(self):
        pass
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

class MockDBConnection:
    def __init__(self):
        self.closed = False
    def cursor(self):
        return MockCursor()
    def commit(self):
        pass
    def rollback(self):
        pass
    def close(self):
        self.closed = True
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

class MockDBPool:
    def __init__(self, *args, **kwargs):
        self.closed = False
        self._conn = MockDBConnection()
    def getconn(self, key=None):
        return self._conn
    def putconn(self, conn, key=None, close=False):
        pass
    def closeall(self):
        self.closed = True

# Define Mock Websocket Client
class MockWebSocket:
    def __init__(self):
        self.sent_messages = []
        self.is_closed = False
        self._loop_count = 0
        
    async def send(self, message):
        self.sent_messages.append(message)
        
    def __aiter__(self):
        return self
        
    async def __anext__(self):
        if self.is_closed or self._loop_count > 5:
            raise StopAsyncIteration
        
        self._loop_count += 1
        await asyncio.sleep(0.1)
        
        # Send initial snapshot then mock deltas
        if self._loop_count == 1:
            return json.dumps({
                "type": "orderbook_snapshot",
                "msg": {
                    "market_ticker": "MOCK_TICKER",
                    "yes": [[50, 10]],
                    "no": [[45, 10]]
                }
            })
        else:
            return json.dumps({
                "type": "orderbook_delta",
                "msg": {
                    "market_ticker": "MOCK_TICKER",
                    "side": "yes",
                    "price": 50,
                    "delta": 5
                }
            })

class MockWSConnectContextManager:
    def __init__(self, *args, **kwargs):
        self.mock_ws = MockWebSocket()
        
    async def __aenter__(self):
        return self.mock_ws
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

@pytest.fixture(autouse=True)
def mock_kalshi_api_layer():
    """
    Autouse fixture that patches all Kalshi REST API endpoints, private key load functions,
    database connection, and the WebSocket connection. Live testing is only enabled
    if KALSHI_LIVE_TESTS=true is explicitly set and valid credentials exist.
    """
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key_demo.pem")
    has_key = bool(os.getenv("KALSHI_PRIVATE_KEY")) or (os.path.exists(key_path) and os.path.getsize(key_path) > 0)
    has_api_key = bool(os.getenv("KALSHI_API_KEY"))
    run_live = (os.getenv("KALSHI_LIVE_TESTS") == "true") and has_key and has_api_key
    
    # By default, mock out the API and DB layers to prevent live capital execution and local DB dependencies
    if not run_live:
        # 1. Mock the auth header generator where it is imported in modules
        mock_headers = {"Authorization": "Bearer mock-token"}
        auth_patchers = [
            patch("data.inventory_manager.get_auth_headers", return_value=mock_headers),
            patch("data.websocket_client.get_auth_headers", return_value=mock_headers),
            patch("execution.order_manager.get_auth_headers", return_value=mock_headers),
            patch("auth.kalshi_auth.get_auth_headers", return_value=mock_headers)
        ]
        for p in auth_patchers:
            p.start()
        
        # 2. Mock HTTP requests
        get_patcher = patch("requests.get", side_effect=lambda url, *args, **kwargs: mock_request_handler("GET", url, *args, **kwargs))
        post_patcher = patch("requests.post", side_effect=lambda url, *args, **kwargs: mock_request_handler("POST", url, *args, **kwargs))
        delete_patcher = patch("requests.delete", side_effect=lambda url, *args, **kwargs: mock_request_handler("DELETE", url, *args, **kwargs))
        
        get_patcher.start()
        post_patcher.start()
        delete_patcher.start()
        
        # 3. Mock the websockets connection
        ws_patcher = patch("websockets.connect", side_effect=MockWSConnectContextManager)
        ws_patcher.start()

        # 4. Mock psycopg2 database connection and connection pool globally to bypass connection errors
        db_patcher = patch("psycopg2.connect", return_value=MockDBConnection())
        db_patcher.start()
        pool_patcher = patch("psycopg2.pool.ThreadedConnectionPool", side_effect=MockDBPool)
        pool_patcher.start()
        try:
            om_pool_patcher = patch("execution.order_manager.pool.ThreadedConnectionPool", side_effect=MockDBPool)
            om_pool_patcher.start()
        except Exception:
            om_pool_patcher = None
        
        yield
        
        for p in auth_patchers:
            p.stop()
        get_patcher.stop()
        post_patcher.stop()
        delete_patcher.stop()
        ws_patcher.stop()
        db_patcher.stop()
        pool_patcher.stop()
        if om_pool_patcher:
            om_pool_patcher.stop()
    else:
        yield
