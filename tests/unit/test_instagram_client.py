"""Offline contract tests for the concrete Instaloader adapter."""

from __future__ import annotations

import io
import json
import pickle
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import ClassVar, Self

import pytest
from instaloader import Instaloader
from instaloader.exceptions import (
    AbortDownloadException,
    ConnectionException,
    LoginRequiredException,
    QueryReturnedNotFoundException,
    TooManyRequestsException,
)
from requests import PreparedRequest, Response, Session
from requests.exceptions import ConnectionError as RequestsConnectionError
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.exceptions import ProtocolError, ReadTimeoutError

from sync.instagram.client import InstaloaderClient
from sync.instagram.errors import (
    AuthenticationError,
    ChallengeError,
    CheckpointError,
    LoginError,
    ThrottleError,
    TransientTransportError,
)
from sync.instagram.models import MediaKind, SavedCandidate, SourceMedia, VerificationStatus
from sync.instagram.retry import RetryPolicy, retry_transport


def _real_client(tmp_path: Path) -> tuple[InstaloaderClient, Instaloader]:
    session_path = tmp_path / "real-session"
    session_path.write_bytes(pickle.dumps({"csrftoken": "offline-csrf"}))
    loader = Instaloader(max_connection_attempts=1)

    def factory(**_options: int) -> Instaloader:
        return loader

    return InstaloaderClient(session_path, loader_factory=factory), loader


class ChunkedRaw(io.BytesIO):
    """A raw body that rejects unbounded reads so the adapter must stream."""

    def read(self, size: int | None = -1, /) -> bytes:
        if size is None or size < 0:
            raise AssertionError("download attempted an unbounded body read")
        return super().read(min(size, 3))


class FailingRaw(ChunkedRaw):
    def __init__(self, error: Exception) -> None:
        super().__init__(b"partial media bytes")
        self.error = error
        self.read_count = 0

    def read(self, size: int | None = -1, /) -> bytes:
        self.read_count += 1
        if self.read_count > 1:
            raise self.error
        return super().read(size)


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.raw = ChunkedRaw(body)
        self.closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed = True
        self.raw.close()


class FailingResponse(FakeResponse):
    def __init__(self, error: Exception) -> None:
        super().__init__(b"")
        self.raw = FailingRaw(error)


class OfflineStatusResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.reason = "Too Many Requests"
        self.url = "https://cdn.example/media.jpg?session=secret-value"

    def json(self) -> dict[str, str]:
        return {"status": "fail", "message": "body=secret-value"}


class OfflineAnonymousSession:
    def __init__(self, response: OfflineStatusResponse) -> None:
        self.response = response

    def get(self, _url: str, *, stream: bool) -> OfflineStatusResponse:
        assert stream
        return self.response


class FakeContext:
    def __init__(self, identity_name: str | None) -> None:
        self.responses: list[FakeResponse | Exception] = []
        self.requested_urls: list[str] = []
        self.identity_name = identity_name
        self.error_log: list[str] = []

    def get_raw(self, url: str) -> FakeResponse:
        self.requested_urls.append(url)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def graphql_query(
        self,
        _query_hash: str,
        _variables: dict[str, object],
        _referer: str | None = None,
    ) -> dict[str, object]:
        return {"data": {"user": {"username": self.identity_name}}}


class FakeLoader:
    def __init__(self, login_name: str | None = "archive_owner") -> None:
        self.context = FakeContext(login_name)
        self.login_name = login_name
        self.load_calls: list[tuple[str, str]] = []
        self.load_error: Exception | None = None

    def load_session_from_file(self, username: str, filename: str) -> None:
        self.load_calls.append((username, filename))
        if self.load_error is not None:
            raise self.load_error

    def test_login(self) -> str | None:
        return self.login_name


class TrackingOwner:
    def __init__(self, *, is_private: bool, username: str = "creator", userid: int = 17) -> None:
        self._is_private = is_private
        self.username = username
        self.userid = userid
        self.privacy_checked = False

    @property
    def is_private(self) -> bool:
        self.privacy_checked = True
        return self._is_private


class FakeSidecarNode:
    def __init__(self, *, is_video: bool, display_url: str, video_url: str | None) -> None:
        self.is_video = is_video
        self.display_url = display_url
        self.video_url = video_url


class TrackingPost:
    def __init__(
        self,
        shortcode: str,
        *,
        owner: TrackingOwner,
        typename: str = "GraphImage",
        image_url: str = "https://cdn.example/image.jpg",
        video_url: str | None = None,
        sidecar_nodes: tuple[FakeSidecarNode, ...] = (),
    ) -> None:
        self.owner_profile = owner
        self._shortcode = shortcode
        self._typename = typename
        self._image_url = image_url
        self._video_url = video_url
        self._sidecar_nodes = sidecar_nodes
        self.sensitive_accessed = False

    def _public(self, value: object) -> object:
        if not self.owner_profile.privacy_checked:
            raise AssertionError("public metadata was read before the privacy decision")
        self.sensitive_accessed = True
        return value

    @property
    def shortcode(self) -> str:
        return str(self._public(self._shortcode))

    @property
    def typename(self) -> str:
        return str(self._public(self._typename))

    @property
    def url(self) -> str:
        return str(self._public(self._image_url))

    @property
    def video_url(self) -> str | None:
        value = self._public(self._video_url)
        return None if value is None else str(value)

    @property
    def caption(self) -> str:
        return str(self._public("A caption"))

    @property
    def date_utc(self) -> datetime:
        value = self._public(datetime(2025, 1, 2, 3, 4, 5))  # noqa: DTZ001
        assert isinstance(value, datetime)
        return value

    def get_sidecar_nodes(self) -> Iterator[FakeSidecarNode]:
        value = self._public(self._sidecar_nodes)
        assert isinstance(value, tuple)
        yield from value


class FakeSavedProfile:
    def __init__(self, posts: tuple[TrackingPost, ...], error: Exception | None = None) -> None:
        self.posts = posts
        self.error = error
        self.iteration_started = False

    def get_saved_posts(self) -> Iterator[TrackingPost]:
        self.iteration_started = True
        if self.error is not None:
            raise self.error
        yield from self.posts


class FakeProfileAPI:
    profile: ClassVar[FakeSavedProfile]
    calls: ClassVar[list[tuple[object, str]]]

    @classmethod
    def from_username(cls, context: object, username: str) -> FakeSavedProfile:
        cls.calls.append((context, username))
        return cls.profile


class FakePostAPI:
    posts: ClassVar[dict[str, TrackingPost | Exception]]
    calls: ClassVar[list[tuple[object, str]]]

    @classmethod
    def from_shortcode(cls, context: object, shortcode: str) -> TrackingPost:
        cls.calls.append((context, shortcode))
        result = cls.posts[shortcode]
        if isinstance(result, Exception):
            raise result
        return result


def _client(loader: FakeLoader, session_path: Path) -> InstaloaderClient:
    FakeProfileAPI.calls = []
    FakePostAPI.calls = []
    FakePostAPI.posts = {}
    return InstaloaderClient(
        session_path,
        loader_factory=lambda **_options: loader,
        profile_type=FakeProfileAPI,
        post_type=FakePostAPI,
    )


def test_client_disables_instaloaders_internal_transport_retries(tmp_path: Path) -> None:
    """Break caught: Instaloader retries a terminal throttle before boundary classification."""
    loader = FakeLoader()
    factory_options: list[dict[str, int]] = []

    def factory(**options: int) -> FakeLoader:
        factory_options.append(options)
        return loader

    InstaloaderClient(
        tmp_path / "session",
        loader_factory=factory,
        profile_type=FakeProfileAPI,
        post_type=FakePostAPI,
    )

    assert factory_options == [{"max_connection_attempts": 1}]


def test_validate_identity_loads_the_requested_session_and_requires_an_exact_match(
    tmp_path: Path,
) -> None:
    """Break caught: a session for a different Instagram identity is accepted."""
    session_path = tmp_path / "session"
    session_path.write_bytes(b"opaque session")
    loader = FakeLoader(login_name="different_owner")
    client = _client(loader, session_path)

    with pytest.raises(LoginError, match="authenticated identity does not match"):
        client.validate_identity("archive_owner")

    assert loader.load_calls == [("archive_owner", str(session_path))]


def test_real_context_logging_is_suppressed_before_identity_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Break caught: upstream URL/body/session diagnostics bypass sanitized boundary errors."""
    client, loader = _real_client(tmp_path)

    def identity_probe(
        _query_hash: str,
        _variables: dict[str, object],
        _referer: str | None = None,
    ) -> dict[str, object]:
        assert _query_hash == "d6f4427fbe92d846298cf93df0b937d3"
        assert _variables == {}
        assert _referer is None
        loader.context.log("https://instagram.example/?session=secret-value")  # type: ignore[no-untyped-call]
        loader.context.error("response body cookie=secret-value")  # type: ignore[no-untyped-call]
        return {"data": {"user": {"username": "archive_owner"}}}

    monkeypatch.setattr(loader.context, "graphql_query", identity_probe)

    client.validate_identity("archive_owner")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert loader.context.error_log == []


@pytest.mark.parametrize(
    ("upstream", "expected_type"),
    [
        pytest.param(
            AbortDownloadException("challenge_required cookie=secret-value"),
            ChallengeError,
            id="challenge",
        ),
        pytest.param(
            TooManyRequestsException("429 body=secret-value"),
            ThrottleError,
            id="throttle",
        ),
        pytest.param(
            ConnectionException("url=https://secret.example/"),
            TransientTransportError,
            id="transient",
        ),
    ],
)
def test_real_identity_probe_preserves_failure_category(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream: Exception,
    expected_type: type[Exception],
) -> None:
    """Break caught: test_login converts a classified identity failure into mismatch login."""
    client, loader = _real_client(tmp_path)

    def fail_identity_probe(
        _query_hash: str,
        _variables: dict[str, object],
        _referer: str | None = None,
    ) -> None:
        assert _query_hash == "d6f4427fbe92d846298cf93df0b937d3"
        assert _variables == {}
        assert _referer is None
        raise upstream

    monkeypatch.setattr(loader.context, "graphql_query", fail_identity_probe)

    with pytest.raises(expected_type) as captured:
        client.validate_identity("archive_owner")

    assert "secret" not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    ("status", "reason", "message", "expected_type"),
    [
        (401, "Unauthorized", "Please log in", LoginError),
        (200, "OK", "login_required", LoginError),
        (400, "Bad Request", "challenge_required", ChallengeError),
    ],
)
def test_real_json_identity_auth_response_is_terminal_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: int,
    reason: str,
    message: str,
    expected_type: type[AuthenticationError],
) -> None:
    """Break caught: real get_json wrappers turn expired authentication into repeated requests."""
    client, loader = _real_client(tmp_path)
    response = Response()
    response.status_code = status
    response.reason = reason
    response.url = "https://www.instagram.com/graphql/query?session=secret-value"
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps({"status": "fail", "message": message}).encode()
    requests: list[str] = []
    sleeps: list[float] = []

    def send(_session: Session, request: PreparedRequest, **_options: object) -> Response:
        requests.append(str(request.url))
        return response

    monkeypatch.setattr(Session, "send", send)
    monkeypatch.setattr(loader.context, "do_sleep", lambda: None)
    monkeypatch.setattr(loader.context._rate_controller, "wait_before_query", lambda _query: None)

    with pytest.raises(expected_type) as captured:
        retry_transport(
            lambda: client.validate_identity("archive_owner"), RetryPolicy(), sleep=sleeps.append
        )

    assert captured.value.exit_code == 20
    assert captured.value.__cause__ is None
    assert len(requests) == 1
    assert sleeps == []
    output = capsys.readouterr()
    assert output.out == output.err == ""
    assert "secret" not in str(captured.value)


def test_iter_saved_is_lazy_and_candidates_do_not_print_their_token(tmp_path: Path) -> None:
    """Break caught: feed access becomes eager or a private candidate token appears in repr output."""
    loader = FakeLoader()
    post = TrackingPost("PRIVATE_CODE", owner=TrackingOwner(is_private=True))
    profile = FakeSavedProfile((post,))
    FakeProfileAPI.profile = profile
    client = _client(loader, tmp_path / "session")
    client.validate_identity("archive_owner")

    candidates = client.iter_saved()

    assert FakeProfileAPI.calls == []
    candidate = next(candidates)
    assert profile.iteration_started
    assert candidate == SavedCandidate(token=post)
    assert "PRIVATE_CODE" not in repr(candidate)


def test_materialize_checks_privacy_before_constructing_public_metadata(tmp_path: Path) -> None:
    """Break caught: metadata from a private owner becomes readable before eligibility is known."""
    loader = FakeLoader()
    private_post = TrackingPost("PRIVATE_CODE", owner=TrackingOwner(is_private=True))
    client = _client(loader, tmp_path / "session")

    result = client.materialize(SavedCandidate(token=private_post))

    assert result is None
    assert not private_post.sensitive_accessed


@pytest.mark.parametrize(
    ("post", "expected_media"),
    [
        pytest.param(
            TrackingPost("IMAGE1", owner=TrackingOwner(is_private=False)),
            [(0, MediaKind.IMAGE, "https://cdn.example/image.jpg")],
            id="image",
        ),
        pytest.param(
            TrackingPost(
                "VIDEO1",
                owner=TrackingOwner(is_private=False),
                typename="GraphVideo",
                video_url="https://cdn.example/video.mp4",
            ),
            [(0, MediaKind.VIDEO, "https://cdn.example/video.mp4")],
            id="video",
        ),
        pytest.param(
            TrackingPost(
                "SIDE1",
                owner=TrackingOwner(is_private=False),
                typename="GraphSidecar",
                sidecar_nodes=(
                    FakeSidecarNode(
                        is_video=False,
                        display_url="https://cdn.example/first.jpg",
                        video_url=None,
                    ),
                    FakeSidecarNode(
                        is_video=True,
                        display_url="https://cdn.example/poster.jpg",
                        video_url="https://cdn.example/second.mp4",
                    ),
                    FakeSidecarNode(
                        is_video=False,
                        display_url="https://cdn.example/third.jpg",
                        video_url=None,
                    ),
                ),
            ),
            [
                (0, MediaKind.IMAGE, "https://cdn.example/first.jpg"),
                (1, MediaKind.VIDEO, "https://cdn.example/second.mp4"),
                (2, MediaKind.IMAGE, "https://cdn.example/third.jpg"),
            ],
            id="sidecar",
        ),
    ],
)
def test_materialize_builds_ordered_public_media(
    tmp_path: Path,
    post: TrackingPost,
    expected_media: list[tuple[int, MediaKind, str]],
) -> None:
    """Break caught: a public image, video, or carousel loses its source kind or ordering."""
    client = _client(FakeLoader(), tmp_path / "session")

    result = client.materialize(SavedCandidate(token=post))

    assert result is not None
    assert result.shortcode == post._shortcode
    assert result.creator_username == "creator"
    assert result.creator_id == 17
    assert result.source_url == f"https://www.instagram.com/p/{post._shortcode}/"
    assert result.caption == "A caption"
    assert result.published_at == datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert [(item.position, item.kind, item.url) for item in result.media] == expected_media


def test_download_streams_into_an_exclusively_created_destination(tmp_path: Path) -> None:
    """Break caught: media is buffered wholesale or an existing destination can be overwritten."""
    loader = FakeLoader()
    response = FakeResponse(b"streamed media bytes")
    loader.context.responses.append(response)
    client = _client(loader, tmp_path / "session")
    post = TrackingPost("IMAGE1", owner=TrackingOwner(is_private=False))
    public = client.materialize(SavedCandidate(token=post))
    assert public is not None
    destination = tmp_path / "download.bin"

    client.download(public.media[0], destination)

    assert destination.read_bytes() == b"streamed media bytes"
    assert loader.context.requested_urls == ["https://cdn.example/image.jpg"]
    assert response.closed
    with pytest.raises(FileExistsError):
        client.download(public.media[0], destination)


def test_real_context_media_429_is_terminal_and_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Break caught: get_raw's plain 429 ConnectionException is treated as retryable."""
    client, loader = _real_client(tmp_path)
    response = OfflineStatusResponse(429)

    @contextmanager
    def anonymous_session() -> Iterator[OfflineAnonymousSession]:
        yield OfflineAnonymousSession(response)

    monkeypatch.setattr(loader.context, "get_anonymous_session", anonymous_session)
    destination = tmp_path / "partial-media"

    with pytest.raises(ThrottleError, match="Instagram throttled") as captured:
        client.download(
            SourceMedia(0, MediaKind.IMAGE, "https://cdn.example/media.jpg"),
            destination,
        )

    assert "secret" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert not destination.exists()


@pytest.mark.parametrize(
    "error_factory",
    [
        pytest.param(
            lambda: RequestsConnectionError("url=https://secret.example/"),
            id="requests-connection",
        ),
        pytest.param(
            lambda: ProtocolError("url=https://secret.example/"),
            id="urllib3-protocol",
        ),
        pytest.param(
            lambda: ReadTimeoutError(
                HTTPConnectionPool("offline.invalid"),
                "https://secret.example/",
                "body=secret-value",
            ),
            id="urllib3-read-timeout",
        ),
    ],
)
def test_stream_failure_is_transient_sanitized_and_removes_partial_file(
    tmp_path: Path,
    error_factory: Callable[[], Exception],
) -> None:
    """Break caught: raw-read transport failure escapes and leaves an unretryable partial file."""
    loader = FakeLoader()
    response = FailingResponse(error_factory())
    loader.context.responses.append(response)
    client = _client(loader, tmp_path / "session")
    destination = tmp_path / "partial-media"

    with pytest.raises(TransientTransportError) as captured:
        client.download(
            SourceMedia(0, MediaKind.VIDEO, "https://cdn.example/video.mp4"),
            destination,
        )

    assert str(captured.value) == "Instagram transport failed"
    assert captured.value.__cause__ is None
    assert not destination.exists()
    assert response.closed


@pytest.mark.parametrize(
    ("is_private", "expected"),
    [
        (False, VerificationStatus.PUBLIC),
        (True, VerificationStatus.PRIVATE),
    ],
)
def test_verify_reports_current_owner_visibility(
    tmp_path: Path, is_private: bool, expected: VerificationStatus
) -> None:
    """Break caught: reconciliation cannot distinguish a public post from a private transition."""
    loader = FakeLoader()
    client = _client(loader, tmp_path / "session")
    FakePostAPI.posts = {
        "VERIFY1": TrackingPost("VERIFY1", owner=TrackingOwner(is_private=is_private))
    }

    assert client.verify("VERIFY1") is expected
    assert FakePostAPI.calls == [(loader.context, "VERIFY1")]


def test_verify_reports_a_definitively_unavailable_post(tmp_path: Path) -> None:
    """Break caught: an Instagram not-found result is mistaken for a transient reconciliation failure."""
    loader = FakeLoader()
    client = _client(loader, tmp_path / "session")
    FakePostAPI.posts = {"MISSING1": QueryReturnedNotFoundException("response-body-secret")}

    assert client.verify("MISSING1") is VerificationStatus.UNAVAILABLE


def test_wrapped_instaloader_throttle_is_still_terminal(tmp_path: Path) -> None:
    """Break caught: Instaloader's 429 wrapper is misclassified as retriable transport."""
    loader = FakeLoader()
    wrapped = ConnectionException("generic wrapper")
    wrapped.__cause__ = TooManyRequestsException("response-body-secret")
    FakeProfileAPI.profile = FakeSavedProfile((), error=wrapped)
    client = _client(loader, tmp_path / "session")
    client.validate_identity("archive_owner")

    with pytest.raises(ThrottleError, match="Instagram throttled") as captured:
        next(client.iter_saved())

    assert captured.value.__cause__ is None


@pytest.mark.parametrize(
    ("upstream", "expected_type", "expected_message"),
    [
        (LoginRequiredException("session-value"), LoginError, "Instagram login failed"),
        (
            AbortDownloadException("checkpoint_required cookie=secret"),
            CheckpointError,
            "Instagram checkpoint required",
        ),
        (
            AbortDownloadException("challenge_required cookie=secret"),
            ChallengeError,
            "Instagram challenge required",
        ),
        (TooManyRequestsException("response-body-secret"), ThrottleError, "Instagram throttled"),
        (
            ConnectionException("response-body-secret"),
            TransientTransportError,
            "Instagram transport failed",
        ),
    ],
)
def test_lazy_feed_failures_are_translated_without_upstream_text(
    tmp_path: Path,
    upstream: Exception,
    expected_type: type[Exception],
    expected_message: str,
) -> None:
    """Break caught: lazy adapter errors bypass classification or leak an Instagram response."""
    loader = FakeLoader()
    FakeProfileAPI.profile = FakeSavedProfile((), error=upstream)
    client = _client(loader, tmp_path / "session")
    client.validate_identity("archive_owner")

    with pytest.raises(expected_type) as captured:
        next(client.iter_saved())

    assert str(captured.value) == expected_message
    assert captured.value.__cause__ is None


def test_load_session_authentication_failure_is_translated_before_transport(
    tmp_path: Path,
) -> None:
    """Break caught: an authentication exception is misclassified as retriable transport."""
    loader = FakeLoader()
    loader.load_error = LoginRequiredException("cookie=secret-session")
    client = _client(loader, tmp_path / "session")

    with pytest.raises(LoginError, match="Instagram login failed") as captured:
        client.validate_identity("archive_owner")

    assert captured.value.__cause__ is None
