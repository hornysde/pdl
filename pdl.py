from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, AsyncGenerator, Literal, Tuple
from urllib.parse import urlparse

import argparse
import asyncio
import datetime
import logging
import re

import aiohttp
import pydantic
import pyffmpeg  # type: ignore[import-untyped]
import tenacity
import tqdm
import yt_dlp  # type: ignore[import-untyped]


class Endpoint:
    @staticmethod
    def _formatted(url_template: str):
        def format(**kwargs):
            return url_template.format(**kwargs)

        return format

    site = "https://www.patreon.com"
    current_user_with_memberships = (
        site
        + "/api/current_user?include=active_memberships.campaign&fields[member]=currently_entitled_amount_cents,access_expires_at"
    )
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
    name: Annotated[
        str, pydantic.Field(validation_alias=pydantic.AliasPath("attributes", "name"))
    ]
    creator_id: Annotated[
        str,
        pydantic.Field(
            validation_alias=pydantic.AliasPath(
                "relationships", "creator", "data", "id"
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
    membership_ids: Annotated[
        list[str],
        pydantic.Field(
            validation_alias=pydantic.AliasPath(
                "relationships", "active_memberships", "data"
            )
        ),
    ] = []

    @pydantic.field_validator("membership_ids", mode="before")
    @classmethod
    def _extract_membership_ids(cls, memberships):
        return [membership["id"] for membership in memberships]


class Membership(pydantic.BaseModel):
    """
    The record of a user's membership to a campaign.
    https://docs.patreon.com/#member
    """

    type: Literal["member"]
    id: str
    access_expires_at: Annotated[
        datetime.datetime | None,
        pydantic.Field(
            validation_alias=pydantic.AliasPath("attributes", "access_expires_at")
        ),
    ]
    currently_entitled_amount_cents: Annotated[
        int,
        pydantic.Field(
            validation_alias=pydantic.AliasPath(
                "attributes", "currently_entitled_amount_cents"
            )
        ),
    ]
    campaign_id: Annotated[
        str,
        pydantic.Field(
            validation_alias=pydantic.AliasPath(
                "relationships", "campaign", "data", "id"
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
            "podcast",
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
    url: Annotated[
        str, pydantic.Field(validation_alias=pydantic.AliasPath("attributes", "url"))
    ]
    iframe_src: Annotated[
        str | None,
        pydantic.Field(
            validation_alias=pydantic.AliasPath("attributes", "embed", "html")
        ),
    ] = None

    @pydantic.field_validator("iframe_src", mode="before")
    @classmethod
    def parse_iframe_src(cls, value: str | None):
        if not value:
            return None
        # Extract src attribute from iframe HTML string
        iframe_match = re.search(
            r'<iframe[^>]*src=["\']([^"\']*)["\']', value, re.IGNORECASE
        )
        if iframe_match:
            return iframe_match.group(1)
        return None


class Media(pydantic.BaseModel):
    id: str
    type: Literal["media"]
    attributes: Attributes

    class Attributes(pydantic.BaseModel):
        # Steaming media application/x-mpegURL as null size.
        size_bytes: int | None
        mimetype: str | None
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
        return (
            self.attributes.image_urls.original
            or self.attributes.image_urls.default
            or self.attributes.download_url
        )

    def get_stream_url(self) -> str | None:
        if self.get_download_url() is not None:
            return None
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

    def make_headers(self) -> dict[str, str]:
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
        response = await self.session.get(url, headers=self.make_headers())
        # Retry on server errors
        if response.status in [429, 502, 503, 504]:
            response.raise_for_status()

        return response


class Patreon:
    @dataclass
    class Membership:
        membership: Membership
        campaign: Campaign
        creator: User

        @property
        def creator_name(self) -> str:
            return self.campaign.name

        @property
        def amount_cent(self) -> int:
            return self.membership.currently_entitled_amount_cents

        @property
        def expiring_in(self) -> datetime.timedelta | None:
            if self.membership.access_expires_at is None:
                return None
            return self.membership.access_expires_at - datetime.datetime.now(
                datetime.timezone.utc
            )

        @property
        def post_link(self) -> str:
            return Endpoint.posts(campaign_id=self.campaign.id, patron_id=self)

    def __init__(self, auth: AuthInfo):
        self.session = Session(auth)
        self.me: User | None = None
        self.memberships: dict[str, Membership] = {}
        self.campaigns: dict[str, Campaign] = {}
        self.creator: dict[str, User] = {}

    async def __aenter__(self):
        await self.session.create()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.session.close()

    async def login(self):
        response = await self.session.get(Endpoint.current_user_with_memberships)
        data = await response.json()
        # Get current user
        self.me = User.model_validate(data["data"])
        # Filter out placeholder entity with id "-1"
        entities = [entity for entity in data["included"] if entity["id"] != "-1"]
        # Parse all included entities (not all are relevant to logged in user)
        self.memberships = {
            entity["id"]: Membership.model_validate(entity)
            for entity in entities
            if entity["type"] == "member"
        }
        self.campaigns = {
            entity["id"]: Campaign.model_validate(entity)
            for entity in entities
            if entity["type"] == "campaign"
        }
        self.creators = {
            entity["id"]: User.model_validate(entity)
            for entity in entities
            if entity["type"] == "user"
        }

        return self.me

    def get_memberships(self) -> list[Patreon.Membership]:
        if self.me is None:
            raise ValueError("Not logged in, call login() first")

        memberships: list[Patreon.Membership] = []
        for membership_id in self.me.membership_ids:
            membership = self.memberships[membership_id]
            campaign = self.campaigns[membership.campaign_id]
            creator = self.creators[campaign.creator_id]
            memberships.append(
                Patreon.Membership(
                    membership=membership,
                    creator=creator,
                    campaign=campaign,
                )
            )
        return memberships

    async def get_posts(
        self, membership: Patreon.Membership
    ) -> AsyncGenerator[Tuple[list[Post], list[Media]]]:
        if self.me is None:
            raise ValueError("Not logged in, call login() first")

        link = Endpoint.posts(campaign_id=membership.campaign.id, patron_id=self.me.id)
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
                for entity in data.get("included", [])
                if entity["type"] == "media"
            ]
            link = data.get("links", {}).get("next")
            yield posts, medias

    async def _download_file(self, url: str, filepath: Path, progress: tqdm.tqdm):
        response = await self.session.get(url)
        response.raise_for_status()
        with open(filepath, "wb") as f:
            async for chunk in response.content.iter_chunked(8192):
                f.write(chunk)
        progress.update()

    async def download_medias(
        self, medias: list[Media], dest_dir: Path, progress: tqdm.tqdm
    ) -> int:
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Prepare download coroutines
        coroutines = []
        for media in medias:
            url = media.get_download_url()
            # Skip if no downloadable url
            if url is None:
                continue

            extension = Path(urlparse(url).path).suffix or ""
            filename = f"{media.id}{extension}"
            filepath = dest_dir / filename
            # Skip if file already exists
            if filepath.exists():
                continue

            coroutines.append(self._download_file(url, filepath, progress))
        # Execute all downloads concurrently
        if coroutines:
            await asyncio.gather(*coroutines)
        return len(coroutines)


def save_stream_media(media: Media, dest_dir: Path) -> bool:
    dest_dir.mkdir(parents=True, exist_ok=True)

    stream_url = media.get_stream_url()
    # Skip if no stream url
    if stream_url is None:
        return False

    filename = f"{media.id}.mp4"
    filepath = dest_dir / filename
    # Skip if file already exists
    if filepath.exists():
        return False

    # Use pyffmpeg to download m3u8 stream
    ffmpeg = pyffmpeg.FFmpeg()
    ffmpeg.options(
        [
            "-headers",
            f'"referer: {Endpoint.site}"',
            "-i",
            stream_url,
            "-c",
            "copy",
            "-y",
            f'"{str(filepath)}"',
        ]
    )
    return True


class EmbedDownloader:
    def __init__(self, browser: str | None):
        class SilentLogger:
            def debug(self, msg):
                pass

            def warning(self, msg):
                pass

            def error(self, msg):
                pass

        ydl_opts = {
            "cookiesfrombrowser": (browser,) if browser else None,
            "ffmpeg_location": pyffmpeg.FFmpeg().get_ffmpeg_bin(),
            # Accept best quality adaptive and progressive formats
            "format": "bestvideo*+bestaudio/best",
            "concurrent_fragment_downloads": 10,
            "logger": SilentLogger(),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "http_headers": {
                "referer": Endpoint.site,
            },
        }
        # Persist yt_dlp session so that it only prompts for cookie release once.
        self.dl = yt_dlp.YoutubeDL(ydl_opts)
        self.dir_to_ids: dict[str, set[str]] = {}

    def is_downloaded(self, post: Post, dest_dir: Path) -> bool:
        if not dest_dir.exists():
            return False
        if str(dest_dir) not in self.dir_to_ids:
            self.dir_to_ids[str(dest_dir)] = {
                f.stem for f in dest_dir.iterdir() if f.is_file()
            }
        downloaded_ids = self.dir_to_ids[str(dest_dir)]
        return post.id in downloaded_ids

    def save_post_embed(self, post: Post, dest_dir: Path) -> bool:
        if post.embed_url is None:
            return False
        if self.is_downloaded(post, dest_dir):
            return False
        dest_dir.mkdir(parents=True, exist_ok=True)
        self.dl.params["outtmpl"] = {"default": f"{dest_dir / post.id}.%(ext)s"}

        # Video provider behavior differs. Try all urls to improve robustness.
        urls = [post.embed_url, post.url]
        if post.iframe_src is not None:
            urls.append(post.iframe_src)
        errors: list[str] = []
        for url in urls:
            try:
                self.dl.download([url])
                return True
            except yt_dlp.utils.DownloadError as e:
                errors.append(url)
                errors.append(str(e))
        print("\n".join(errors))
        return False


async def download_membership(
    api: Patreon,
    embed_downloader: EmbedDownloader,
    membership: Patreon.Membership,
    download_dir: Path,
):
    # Index all posts
    all_posts = []
    all_medias = []
    with tqdm.tqdm(desc="Indexing", unit="post", leave=False) as progress:
        async for posts, medias in api.get_posts(membership):
            all_posts.extend(posts)
            all_medias.extend(medias)
            progress.update(len(posts))

    def print_download_stat(name: str, count: int | str, total: int):
        print(f"{name:<12} {count:>5}/{total}")

    # Save downloadable medias
    downloadable_medias = [
        media for media in all_medias if media.get_download_url() is not None
    ]
    with tqdm.tqdm(
        desc="Downloadable media",
        total=len(downloadable_medias),
        unit="file",
        leave=False,
    ) as progress:
        count = await api.download_medias(downloadable_medias, download_dir, progress)
    print_download_stat("downloadable", count, len(downloadable_medias))

    # Save streaming medias
    streaming_medias = [
        media for media in all_medias if media.get_stream_url() is not None
    ]
    with tqdm.tqdm(
        desc="Streaming media", total=len(streaming_medias), unit="file", leave=False
    ) as progress:
        count = 0
        for media in streaming_medias:
            if save_stream_media(media, download_dir):
                count += 1
            progress.update()
    print_download_stat("streaming", count, len(streaming_medias))

    # Save embedded medias
    embedded_posts = [post for post in all_posts if post.embed_url is not None]
    with tqdm.tqdm(
        desc="Embedded media", total=len(embedded_posts), unit="file", leave=False
    ) as progress:
        count = 0
        for post in embedded_posts:
            if embed_downloader.save_post_embed(post, download_dir):
                count += 1
            progress.update()
    print_download_stat("embedded", count, len(embedded_posts))


async def async_main():
    # Shut pyffmpeg up
    logging.getLogger("pyffmpeg").handlers = []

    parser = argparse.ArgumentParser(description="Brutally simple Patreon downloader.")
    parser.add_argument(
        "--config",
        default="config.json",
        help="Config file path (default: config.json)",
    )
    parser.add_argument(
        "--output", default="downloads", help="Download directory (default: downloads)"
    )
    parser.add_argument(
        "--browser",
        default=None,
        help="Browser to extract cookies from for yt-dlp (default: None).",
    )
    args = parser.parse_args()

    with open(args.config, "r") as f:
        auth = AuthInfo.model_validate_json(f.read())
    async with Patreon(auth) as api:
        user = await api.login()
        print(f"Logged in as {user.full_name}")
        embed_downloader = EmbedDownloader(args.browser)
        for membership in api.get_memberships():
            download_dir = Path(args.output) / membership.creator_name
            print(f"${membership.amount_cent / 100.0:>7.2f} {membership.creator_name}")
            if expiring_in := membership.expiring_in:
                days = expiring_in.days
                print(f"(Expiring in {days} days)")
            await download_membership(api, embed_downloader, membership, download_dir)


def main():
    """pdl command entry point."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
