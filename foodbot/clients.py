from urllib.parse import urlparse


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


async def shopping_link(client, settings, items):
    host = ("https://connect.instacart.com" if settings.instacart_env == "production"
            else "https://connect.dev.instacart.tools")
    response = await client.post(
        f"{host}/idp/v1/products/products_link",
        headers={"Authorization": f"Bearer {settings.instacart_key}"},
        json={"title": "My groceries", "link_type": "shopping_list", "line_items": [
            {"name": x["name"], "line_item_measurements": [
                {"quantity": x["quantity"], "unit": x["unit"]}]} for x in items
        ]},
    )
    response.raise_for_status()
    url = response.json()["products_link_url"]
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Invalid shopping URL")
    return url
