"""
Kalshi WebSocket Client

This module establishes and manages a persistent connection to the Kalshi V2 WebSocket 
interface. It handles automatic reconnection with exponential backoff, signs connection 
requests using RSA keys, routes messages to handlers, and manages market subscriptions.
"""

import asyncio
import json
import logging
import websockets
import ssl
import certifi
from typing import Callable, Awaitable, Dict, Any



from config import ENVIRONMENT
from auth.kalshi_auth import get_auth_headers

# Set up logging for the client
logger = logging.getLogger("KalshiWS")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

class KalshiWebsocketClient:
    def __init__(self):
        if ENVIRONMENT == "prod":
            self.ws_url = "wss://api.elections.kalshi.com/trade-api/ws/v2"
        else:
            self.ws_url = "wss://demo-api.kalshi.co/trade-api/ws/v2"
            
        self.ws_connection = None
        self.message_handlers = []
        self.subscription_requests = []
        self.is_connected = False
        self._msg_id = 1
        
    def add_message_handler(self, handler: Callable[[Dict[str, Any]], Awaitable[None]]):
        """Register a handler for incoming websocket messages."""
        self.message_handlers.append(handler)

    async def connect(self):
        """Establish the WebSocket connection to Kalshi."""
        logger.info(f"Connecting to {self.ws_url}")
        
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        
        max_reconnect_delay = 60
        reconnect_delay = 1
        
        while True:
            try:
                # Generate fresh auth headers for each connection attempt to prevent signature timeouts
                headers = get_auth_headers("GET", "/trade-api/ws/v2")
                
                # Disable ping_interval for now if Kalshi server uses a custom ping mechanism,
                # though websockets default ping often works fine.
                async with websockets.connect(self.ws_url, additional_headers=headers, ssl=ssl_context) as websocket:
                    self.ws_connection = websocket
                    self.is_connected = True
                    reconnect_delay = 1 # Reset backoff on successful connection
                    logger.info("Connected successfully.")
                    
                    # Send all queued subscriptions upon successful connect
                    for sub in self.subscription_requests:
                        await self.send_message(sub)
                    
                    # Listen for incoming text messages
                    async for message in websocket:
                        try:
                            data = json.loads(message)
                            for handler in self.message_handlers:
                                asyncio.create_task(handler(data))
                        except Exception as e:
                            logger.error(f"Error handling message: {e}")
                            
            except websockets.exceptions.ConnectionClosed as e:
                self.is_connected = False
                logger.warning(f"Connection closed. Reconnecting in {reconnect_delay} seconds... ({e})")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
            except Exception as e:
                self.is_connected = False
                logger.error(f"WebSocket error: {e}. Reconnecting in {reconnect_delay} seconds...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

    async def send_message(self, message: dict):
        """Send a JSON payload over the socket."""
        if self.is_connected and self.ws_connection:
            await self.ws_connection.send(json.dumps(message))
        else:
            logger.warning("Not connected. Queuing message to send upon connection.")
            self.subscription_requests.append(message)
            
    async def subscribe(self, channels: list[str], market_tickers: list[str] = None):
        """Helper method to subscribe to channels like orderbook or fill."""
        msg = {
            "id": self._msg_id,
            "cmd": "subscribe",
            "params": {
                "channels": channels
            }
        }
        if market_tickers:
            msg["params"]["market_tickers"] = market_tickers
            
        self._msg_id += 1
        await self.send_message(msg)
