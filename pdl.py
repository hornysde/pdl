from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

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
    current_user_with_pledges = site + "/api/current_user?include=pledges"


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


class Campaign(pydantic.BaseModel):
    """A creator's page/channel for publishing content."""

    type: Literal["campaign"]
    id: str


class Reward(pydantic.BaseModel):
    """Subscription tier offered by a campaign with price and benefits."""

    type: Literal["reward"]
    id: str
    post_count: Annotated[
        int,
        pydantic.Field(validation_alias=pydantic.AliasPath("attributes", "post_count")),
    ]
    campaign_id: Annotated[
        str,
        pydantic.Field(
            validation_alias=pydantic.AliasPath(
                "relationships", "campaign", "data", "id"
            )
        ),
    ]


class User(pydantic.BaseModel):
    """Individual person on Patreon (creator or patron)."""

    type: Literal["user"]
    id: str
    full_name: str = pydantic.Field(
        validation_alias=pydantic.AliasPath("attributes", "full_name")
    )
    image_url: str = pydantic.Field(
        validation_alias=pydantic.AliasPath("attributes", "image_url")
    )
    pledge_ids: Annotated[
        list[str],
        pydantic.Field(
            validation_alias=pydantic.AliasPath("relationships", "pledges", "data")
        ),
    ] = []

    @pydantic.field_validator("pledge_ids", mode="before")
    @classmethod
    def _extract_pledge_ids(cls, pledges):
        return [pledge["id"] for pledge in pledges]


class Pledge(pydantic.BaseModel):
    """Active subscription/payment from a patron to a creator for a specific reward tier."""

    type: Literal["pledge"]
    id: str
    amount_cents: int = pydantic.Field(
        validation_alias=pydantic.AliasPath("attributes", "amount_cents")
    )
    reward_id: str = pydantic.Field(
        validation_alias=pydantic.AliasPath("relationships", "reward", "data", "id")
    )
    creator_id: str = pydantic.Field(
        validation_alias=pydantic.AliasPath("relationships", "creator", "data", "id")
    )


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


class Patreon:
    @dataclass
    class Pledge:
        pledge: Pledge
        reward: Reward
        creator: User
        campaign: Campaign

        @property
        def creator_name(self) -> str:
            return self.creator.full_name

        @property
        def amount_cent(self) -> int:
            return self.pledge.amount_cents

    def __init__(self, auth: AuthInfo):
        self.session = Session(auth)
        self.me: User | None = None
        self.pledges: dict[str, Pledge] = {}
        self.creators: dict[str, User] = {}
        self.rewards: dict[str, Reward] = {}
        self.campaigns: dict[str, Campaign] = {}

    async def __aenter__(self):
        await self.session.create()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.session.close()

    async def login(self):
        response = await self.session.get(Endpoint.current_user_with_pledges)
        data = await response.json()
        # Get current user
        self.me = User.model_validate(data["data"])
        # Filter out placeholder entity with id "-1"
        entities = [entity for entity in data["included"] if entity["id"] != "-1"]
        # Parse all included entities (not all are relevant to logged in user)
        self.pledges = {
            entity["id"]: Pledge.model_validate(entity)
            for entity in entities
            if entity["type"] == "pledge"
        }
        self.creators = {
            entity["id"]: User.model_validate(entity)
            for entity in entities
            if entity["type"] == "user"
        }
        self.rewards = {
            entity["id"]: Reward.model_validate(entity)
            for entity in entities
            if entity["type"] == "reward"
        }
        self.campaigns = {
            entity["id"]: Campaign.model_validate(entity)
            for entity in entities
            if entity["type"] == "campaign"
        }

        return self.me

    def get_pledges(self) -> list[Patreon.Pledge]:
        if self.me is None:
            raise ValueError("Not logged in, call login() first")

        pledges = []
        for pledge_id in self.me.pledge_ids:
            pledge = self.pledges[pledge_id]
            reward = self.rewards[pledge.reward_id]
            creator = self.creators[pledge.creator_id]
            campaign = self.campaigns[reward.campaign_id]
            pledges.append(
                Patreon.Pledge(
                    pledge=pledge,
                    reward=reward,
                    creator=creator,
                    campaign=campaign,
                )
            )
        return pledges


async def async_main():
    config_file_path = "config.json"
    with open(config_file_path, "r") as f:
        auth = AuthInfo.model_validate_json(f.read())
    async with Patreon(auth) as patreon:
        user = await patreon.login()
        print(f"Logged in as {user.full_name}")
        print("Subscribed to:")
        for pledge in patreon.get_pledges():
            print(f"${pledge.amount_cent / 100.0:>7.2f} {pledge.creator_name}")


def main():
    """pdl command entry point."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
