from __future__ import annotations

import asyncio

import aiohttp
import pydantic
import tenacity


class Endpoint:
    @staticmethod
    def _formatted(url_template: str):
        def format(**kwargs):
            return url_template.format(**kwargs)

        return format

    site = "https://www.patreon.com"
    current_user = site + "/api/current_user"


class AuthInfo(pydantic.BaseModel):
    cookie: dict[str, str]
    user_agent: str

    @pydantic.field_validator("cookie", mode="before")
    @classmethod
    def parse_cookie(cls, value):
        if not isinstance(value, str):
            return value
        # Parse ; separated and = connected kv pairs
        return dict(
            item.strip().split("=", 1) for item in value.split(";") if "=" in item
        )

    def make_header(self) -> dict[str, str]:
        return {"user-agent": self.user_agent}


class Session:
    def __init__(self, auth: AuthInfo):
        self.session: aiohttp.ClientSession | None = None
        self.auth = auth

    async def create(self):
        await self.close()
        self.session = aiohttp.ClientSession(cookies=self.auth.cookie)

    async def close(self):
        if self.session is None:
            return
        await self.session.close()
        self.session = None

    def make_headers(self, url: str) -> dict[str, str]:
        headers = {
            "referer": Endpoint.site,
            "accept": "*/*",
            "connection": "keep-alive",
        }
        headers.update(self.auth.make_header())
        return headers

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(5),
        wait=tenacity.wait_exponential(multiplier=1, min=1, max=10),
        retry=tenacity.retry_if_exception_type(aiohttp.ClientError),
        reraise=True,
    )
    async def get(self, url: str) -> aiohttp.ClientResponse:
        assert self.session is not None
        response = await self.session.get(url, headers=self.make_headers(url))
        # Retry on server errors
        if response.status in [429, 502, 503, 504]:
            response.raise_for_status()

        return response


async def main():
    config_file_path = "config.json"
    with open(config_file_path, "r") as f:
        auth = AuthInfo.model_validate_json(f.read())
    session = Session(auth)
    await session.create()
    response = await session.get(Endpoint.current_user)
    print(response.status)
    print(await response.text())
    await session.close()


if __name__ == "__main__":
    asyncio.run(main())
