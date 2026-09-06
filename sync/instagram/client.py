"""Concrete Instaloader implementation of the sanitized Instagram boundary."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, NoReturn, Protocol, Self, cast

from instaloader import Instaloader, Post, Profile
from instaloader import exceptions as instaloader_errors
from requests.exceptions import RequestException
from urllib3.exceptions import HTTPError

from .errors import (
    ArchiveError,
    LoginError,
    TransientTransportError,
    UnavailablePostError,
    ValidationError,
    translate_instaloader_error,
)
from .models import MediaKind, PublicPost, SavedCandidate, SourceMedia, VerificationStatus

_COPY_BLOCK_SIZE = 1024 * 1024
_IDENTITY_QUERY_HASH = "d6f4427fbe92d846298cf93df0b937d3"


class _RawResponse(Protocol):
    raw: BinaryIO

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> object: ...


class _Context(Protocol):
    def get_raw(self, url: str) -> _RawResponse: ...

    def graphql_query(
        self,
        query_hash: str,
        variables: dict[str, object],
        referer: str | None = None,
    ) -> dict[str, object]: ...


class _Loader(Protocol):
    context: _Context

    def load_session_from_file(self, username: str, filename: str) -> None: ...


class _LoaderFactory(Protocol):
    def __call__(self, *, max_connection_attempts: int) -> _Loader: ...


class _LoggingContext(Protocol):
    log: Callable[..., None]
    error: Callable[..., None]
    error_log: list[str]


class _OwnerProfile(Protocol):
    is_private: bool
    username: str
    userid: int


class _SidecarNode(Protocol):
    is_video: bool
    display_url: str
    video_url: str | None


class _Post(Protocol):
    owner_profile: _OwnerProfile
    shortcode: str
    typename: str
    url: str
    video_url: str | None
    caption: str | None
    date_utc: datetime

    def get_sidecar_nodes(self) -> Iterator[_SidecarNode]: ...


class _SavedProfile(Protocol):
    def get_saved_posts(self) -> Iterator[_Post]: ...


class _ProfileType(Protocol):
    def from_username(self, context: object, username: str) -> _SavedProfile: ...


class _PostType(Protocol):
    def from_shortcode(self, context: object, shortcode: str) -> _Post: ...


class InstaloaderClient:
    """Adapt one authenticated Instaloader session without exposing private candidates."""

    def __init__(
        self,
        session_path: Path,
        *,
        loader_factory: object = Instaloader,
        profile_type: object = Profile,
        post_type: object = Post,
    ) -> None:
        factory = cast(_LoaderFactory, loader_factory)
        self._loader = factory(max_connection_attempts=1)
        _suppress_context_logging(self._loader.context)
        self._session_path = session_path
        self._profile_type = cast(_ProfileType, profile_type)
        self._post_type = cast(_PostType, post_type)
        self._username: str | None = None

    def validate_identity(self, username: str) -> None:
        try:
            self._loader.load_session_from_file(username, str(self._session_path))
            authenticated_username = _identity_username(self._loader.context)
        except _TRANSLATABLE_ERRORS as error:
            _raise_translated(error)
        if authenticated_username != username:
            raise LoginError("authenticated identity does not match requested username")
        self._username = username

    def iter_saved(self) -> Iterator[SavedCandidate]:
        if self._username is None:
            raise LoginError("Instagram identity has not been validated")
        try:
            profile = self._profile_type.from_username(self._loader.context, self._username)
            posts = iter(profile.get_saved_posts())
        except _TRANSLATABLE_ERRORS as error:
            _raise_translated(error)
        while True:
            try:
                post = next(posts)
            except StopIteration:
                return
            except _TRANSLATABLE_ERRORS as error:
                _raise_translated(error)
            yield SavedCandidate(token=post)

    def materialize(self, candidate: SavedCandidate) -> PublicPost | None:
        post = cast(_Post, candidate.token)
        try:
            owner = post.owner_profile
            if owner.is_private:
                return None
            shortcode = post.shortcode
            media = _source_media(post)
            published_at = _as_utc(post.date_utc)
            return PublicPost(
                shortcode=shortcode,
                creator_username=owner.username,
                creator_id=owner.userid,
                source_url=f"https://www.instagram.com/p/{shortcode}/",
                caption=post.caption or "",
                published_at=published_at,
                media=media,
            )
        except _TRANSLATABLE_ERRORS as error:
            _raise_translated(error)

    def download(self, source: SourceMedia, destination: Path) -> None:
        created = False
        completed = False
        try:
            with destination.open("xb") as output:
                created = True
                with self._loader.context.get_raw(source.url) as response:
                    shutil.copyfileobj(response.raw, output, length=_COPY_BLOCK_SIZE)
            completed = True
        except FileExistsError:
            raise
        except _TRANSLATABLE_ERRORS as error:
            _raise_translated(error)
        except OSError:
            raise
        finally:
            if created and not completed:
                destination.unlink(missing_ok=True)

    def verify(self, shortcode: str) -> VerificationStatus:
        try:
            post = self._post_type.from_shortcode(self._loader.context, shortcode)
            return (
                VerificationStatus.PRIVATE
                if post.owner_profile.is_private
                else VerificationStatus.PUBLIC
            )
        except _TRANSLATABLE_ERRORS as error:
            translated = translate_instaloader_error(error)
            if isinstance(translated, UnavailablePostError):
                return VerificationStatus.UNAVAILABLE
            _raise_translated(error)


def _source_media(post: _Post) -> tuple[SourceMedia, ...]:
    typename = post.typename
    if typename == "GraphImage":
        return (SourceMedia(position=0, kind=MediaKind.IMAGE, url=post.url),)
    if typename == "GraphVideo":
        video_url = post.video_url
        if video_url is None:
            raise UnavailablePostError("Instagram post has no downloadable media")
        return (SourceMedia(position=0, kind=MediaKind.VIDEO, url=video_url),)
    if typename == "GraphSidecar":
        media: list[SourceMedia] = []
        for position, node in enumerate(post.get_sidecar_nodes()):
            if node.is_video:
                video_url = node.video_url
                if video_url is None:
                    raise UnavailablePostError("Instagram post has no downloadable media")
                media.append(SourceMedia(position, MediaKind.VIDEO, video_url))
            else:
                media.append(SourceMedia(position, MediaKind.IMAGE, node.display_url))
        if not media:
            raise UnavailablePostError("Instagram post has no downloadable media")
        return tuple(media)
    raise ValidationError("Instagram post has an unsupported media type")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _identity_username(context: _Context) -> str | None:
    response = context.graphql_query(_IDENTITY_QUERY_HASH, {})
    data = response.get("data")
    if not isinstance(data, dict):
        return None
    user = data.get("user")
    if not isinstance(user, dict):
        return None
    username = user.get("username")
    return username if isinstance(username, str) else None


def _raise_translated(error: Exception) -> NoReturn:
    translated = translate_instaloader_error(error)
    if translated is None:
        raise error
    raise translated from None


def _suppress_context_logging(context: object) -> None:
    logging_context = cast(_LoggingContext, context)
    logging_context.log = _discard_log
    logging_context.error = _discard_log
    logging_context.error_log.clear()


def _discard_log(*_message: object, **_options: object) -> None:
    pass


_TRANSLATABLE_ERRORS = (
    instaloader_errors.InstaloaderException,
    instaloader_errors.AbortDownloadException,
    ArchiveError,
    TransientTransportError,
    UnavailablePostError,
    RequestException,
    HTTPError,
)
