"""
Kalshi API Authentication Helper

This module handles cryptographic signature generation for authentication with Kalshi's V2 API. 
It loads the private RSA key (.pem file) and generates the required headers (including the timestamp, 
API key, and RSA-PSS signature) for each REST and WebSocket request.
"""

# Import standard libraries for encoding, time management, and path resolution
import base64
import datetime
from pathlib import Path

# Import cryptographic libraries for RSA-PSS signature generation
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from config import API_KEY, PRIVATE_KEY_PATH


_private_key = None

def get_auth_headers(method: str, sign_path: str) -> dict:
    """
    Generates Kalshi API authentication headers using RSA-PSS signing.

    Args:
        method (str): The HTTP method (e.g., 'GET', 'POST').
        sign_path (str): The endpoint path without query parameters.

    Returns:
        dict: A dictionary containing the required Kalshi authentication headers.
    """
    global _private_key
    # Load the private key into memory only once
    if _private_key is None:
        import os
        env_key = os.getenv("KALSHI_PRIVATE_KEY")
        if env_key:
            _private_key = load_pem_private_key(env_key.encode('utf-8'), password=None)
        else:
            with open(PRIVATE_KEY_PATH, "rb") as key_file:
                _private_key = load_pem_private_key(key_file.read(), password=None)

    # 4. Generate the current timestamp in milliseconds
    timestamp_str = str(int(datetime.datetime.now().timestamp() * 1000))

    # 5. Build the exact string required by Kalshi: timestamp + method + path
    msg_string = timestamp_str + method + sign_path
    message_bytes = msg_string.encode('utf-8')

    # 6. Generate the cryptographic signature using RSA-PSS and SHA256
    signature_bytes = _private_key.sign(
        message_bytes,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )

    # 7. Convert the raw bytes to a base64 encoded string
    signature_b64 = base64.b64encode(signature_bytes).decode('utf-8')

    # 8. Return the fully constructed header dictionary
    return {
        "KALSHI-ACCESS-KEY": API_KEY,
        "KALSHI-ACCESS-SIGNATURE": signature_b64,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_str
    }