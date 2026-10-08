import json

def encode(message: dict) -> str:
    return json.dumps(message, ensure_ascii=False)

def decode(message: str) -> dict:
    return json.loads(message)
