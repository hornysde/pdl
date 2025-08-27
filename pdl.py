from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, AsyncGenerator, Literal, Tuple
from urllib.parse import urlparse

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
    posts = _formatted(
        site
        + "/api/posts?filter[campaign_id]={campaign_id}&filter[accessible_by_user_id]={patron_id}&include=attachments,attachments_media,media"
    )


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
    full_name: Annotated[
        str,
        pydantic.Field(validation_alias=pydantic.AliasPath("attributes", "full_name")),
    ]
    image_url: Annotated[
        str,
        pydantic.Field(validation_alias=pydantic.AliasPath("attributes", "image_url")),
    ]
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
    amount_cents: Annotated[
        int,
        pydantic.Field(
            validation_alias=pydantic.AliasPath("attributes", "amount_cents")
        ),
    ]
    patron_id: Annotated[
        str,
        pydantic.Field(
            validation_alias=pydantic.AliasPath("relationships", "patron", "data", "id")
        ),
    ]
    reward_id: Annotated[
        str,
        pydantic.Field(
            validation_alias=pydantic.AliasPath("relationships", "reward", "data", "id")
        ),
    ]
    creator_id: Annotated[
        str,
        pydantic.Field(
            validation_alias=pydantic.AliasPath(
                "relationships", "creator", "data", "id"
            )
        ),
    ]


class Post(pydantic.BaseModel):
    id: str
    type: Literal["post"]
    # Strict parsing to surface any unexpected post types. New types need screening to avoid missing any downloadable.
    # If you encounter crash here, please open an issue. Remove this line to suppress the error.
    post_type: Annotated[
        Literal[
            "link",
            "text_only",
            "image_file",
            "video_external_file",
            "video_embed",
            "poll",
            "audio_file",
        ],
        pydantic.Field(validation_alias=pydantic.AliasPath("attributes", "post_type")),
    ]
    # `link` and `video_embed` posts have embed url.
    embed_url: Annotated[
        str | None,
        pydantic.Field(
            validation_alias=pydantic.AliasPath("attributes", "embed", "url")
        ),
    ] = None


class Media(pydantic.BaseModel):
    id: str
    type: Literal["media"]
    attributes: Attributes

    class Attributes(pydantic.BaseModel):
        # Steaming media application/x-mpegURL as null size.
        size_bytes: int | None
        mimetype: str
        media_type: str | None
        download_url: str | None
        image_urls: ImageURLs | None

        class ImageURLs(pydantic.BaseModel):
            # priority 0
            original: str | None = None
            # priority 1
            default: str | None = None
            # lower qualify than download_url, not used
            default_small: str | None = None

        display: Display | None

        class Display(pydantic.BaseModel):
            url: str | None = None

    def get_download_url(self) -> str | None:
        if self.attributes.image_urls is None:
            return self.attributes.download_url
        return self.attributes.image_urls.original or self.attributes.image_urls.default

    def get_stream_url(self) -> str | None:
        if self.attributes.display is None:
            return None
        return self.attributes.display.url


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

        @property
        def post_link(self) -> str:
            return Endpoint.posts(
                campaign_id=self.campaign.id, patron_id=self.pledge.patron_id
            )

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

    async def get_posts(
        self, pledge: Pledge
    ) -> AsyncGenerator[Tuple[list[Post], list[Media]]]:
        link = pledge.post_link
        while link:
            response = await self.session.get(link)
            data = await response.json()
            posts = [
                Post.model_validate(entity)
                for entity in data["data"]
                if entity["type"] == "post"
            ]
            medias = [
                Media.model_validate(entity)
                for entity in data["included"]
                if entity["type"] == "media"
            ]
            link = data.get("links", {}).get("next")
            yield posts, medias

    async def _download_file(self, url: str, filepath: Path):
        response = await self.session.get(url)
        response.raise_for_status()
        with open(filepath, "wb") as f:
            async for chunk in response.content.iter_chunked(8192):
                f.write(chunk)

    async def download_medias(self, medias: list[Media], dest_dir: Path):
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Prepare download coroutines
        coroutines = []
        for media in medias:
            url = media.get_download_url()
            # Skip if no downloadable url
            if url is None:
                continue

            extension = Path(urlparse(url).path).suffix or ".bin"
            filename = f"{media.id}{extension}"
            filepath = dest_dir / filename
            # Skip if file already exists
            if filepath.exists():
                continue

            coroutines.append(self._download_file(url, filepath))
        # Execute all downloads concurrently
        if coroutines:
            await asyncio.gather(*coroutines)

    async def download_post_embed(self, post: Post, dest_dir: str):
        pass


async def async_main():
    config_file_path = "config.json"
    with open(config_file_path, "r") as f:
        auth = AuthInfo.model_validate_json(f.read())
    async with Patreon(auth) as api:
        user = await api.login()
        print(f"Logged in as {user.full_name}")
        print("Subscribed to:")
        for pledge in api.get_pledges():
            print(f"${pledge.amount_cent / 100.0:>7.2f} {pledge.creator_name}")
            async for posts, medias in api.get_posts(pledge):
                print(f"Found {len(posts)} posts and {len(medias)} media items")
                await api.download_medias(
                    medias, Path("downloads") / pledge.creator_name
                )


def main():
    """pdl command entry point."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
