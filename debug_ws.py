import asyncio, json, time, base64, os
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
import websockets

KALSHI_API_KEY = os.environ.get("KALSHI_API_KEY")
KALSHI_PRIVATE_KEY_PATH = "keys/kalshi_private.pem"

def sign_pss_text(private_key, text):
    message = text.encode("utf-8")
    signature = private_key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    return base64.b64encode(signature).decode("utf-8")

def create_ws_headers(private_key, method="GET", path="/trade-api/ws/v2"):
    timestamp = str(int(time.time() * 1000))
    msg_string = timestamp + method + path
    signature = sign_pss_text(private_key, msg_string)
    return {"KALSHI-ACCESS-KEY": KALSHI_API_KEY, "KALSHI-ACCESS-SIGNATURE": signature, "KALSHI-ACCESS-TIMESTAMP": timestamp}

async def debug():
    with open(KALSHI_PRIVATE_KEY_PATH, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
    
    headers = create_ws_headers(private_key)
    ws_url = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    
    async with websockets.connect(ws_url, additional_headers=headers) as ws:
        await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {"channels": ["trade"]}}))
        
        for i in range(5):
            msg = await ws.recv()
            data = json.loads(msg)
            if data.get("type") == "trade":
                print(f"\n🔍 Trade message {i+1}:")
                print(json.dumps(data, indent=2))
                break

asyncio.run(debug())
