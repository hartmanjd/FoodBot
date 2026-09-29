class Telegram:
    def __init__(self, client, token):
        self.client, self.base = client, f"https://api.telegram.org/bot{token}"

    async def call(self, method, payload):
        response = await self.client.post(f"{self.base}/{method}", json=payload)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise ValueError("Telegram rejected request")
        return data["result"]

    async def send(self, owner, payload):
        return await self.call("sendMessage", {"chat_id": owner, **payload})
