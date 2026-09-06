# Instagram Saved Archive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and verify a GitHub-only archive that ingests eligible saved Instagram posts into an atomic snapshot and publishes an accessible Astro feed through GitHub Pages.

**Architecture:** A typed Python 3.12 core owns archive validation, transactional synchronization, the Instaloader boundary, deterministic media processing, and safe snapshot publication. Astro 7 consumes a validated snapshot at build time and ships static HTML plus framework-free TypeScript for feed interactions; GitHub Actions is the only scheduler and deployment runtime.

**Tech Stack:** Python 3.12, Instaloader 4.15.3, Pillow 12.3.0, FFmpeg 6+, pytest 9.1.1, Ruff 0.16.6, mypy 2.3.1, Astro 7.3.1, TypeScript 6.0.3, Vitest 5.0.0, Playwright 1.63.0, Node 24, GitHub Actions, GitHub Pages.

**Spec:** `docs/superpowers/specs/2026-09-06-instagram-saved-archive-design.md`

## Global Constraints

- The archive contains fewer than 200 saved posts at launch.
- The generated GitHub Pages artifact must remain below 900 MB; warn at 850,000,000 bytes and reject at 900,000,000 bytes.
- No generated file may exceed `95 * 1024 * 1024` bytes.
- The public site is static, public, read-only, and contains no private-post metadata.
- `main` contains code and configuration; `archive-data` contains exactly one reachable root snapshot commit.
- Instagram operations are serialized; login, checkpoint, challenge, and throttle failures are never retried.
- Password login and automatic refreshed-session persistence are forbidden in CI.
- All durable timestamps use UTC ISO 8601 with a trailing `Z`.
- All manifest paths are relative POSIX paths under the snapshot root.
- Tests never contact Instagram.
- Production code follows a red-green-refactor cycle; configuration-only scaffolding is folded into the first tested deliverable.
- Python runtime dependencies are pinned exactly to Instaloader 4.15.3 and Pillow 12.3.0.
- Site dependencies are pinned exactly and installed with `npm ci` from `site/package-lock.json`.

---

## File map

### Python package

- `pyproject.toml` — pinned Python metadata, console entry point, test/lint/type-check configuration.
- `sync/archive/models.py` — immutable archive domain records and strict JSON conversion.
- `sync/archive/validation.py` — cross-record validation, paths, hashes, timestamps, and schema upgrades.
- `sync/archive/store.py` — snapshot initialization, atomic JSON writes, and full on-disk validation.
- `sync/archive/removals.py` — canonical removal-list parsing and application.
- `sync/archive/budget.py` — per-file and aggregate size decisions.
- `sync/archive/publisher.py` — hardcoded orphan publication to `archive-data` using a temporary repository.
- `sync/instagram/models.py` — sanitized candidate, source-media, and verification types.
- `sync/instagram/protocol.py` — injectable Instagram client protocol.
- `sync/instagram/errors.py` — stable failure categories and exit codes.
- `sync/instagram/retry.py` — capped exponential backoff with injectable sleep and jitter.
- `sync/instagram/client.py` — the only Instaloader-specific adapter.
- `sync/media/images.py` — EXIF-aware WebP processing and image validation.
- `sync/media/videos.py` — FFprobe inspection, FFmpeg command construction, encoding, and poster generation.
- `sync/media/processor.py` — atomic all-items media processing for one post.
- `sync/engine.py` — discovery, idempotency, bounded backfill, explicit removals, and reconciliation.
- `sync/reporting.py` — redaction and aggregate/job-summary rendering.
- `sync/site_input/exporter.py` — validated snapshot export into `site/public/archive`.
- `sync/bootstrap_session.py` — local interactive session creation and identity validation.
- `sync/cli.py` — command composition and stable process exits.

### Static site

- `site/src/data/archive.ts` — strict build-time manifest loading and script-safe JSON encoding.
- `site/src/lib/feed-state.ts` — pure URL/search/filter/sort state functions.
- `site/src/lib/render-post.ts` — safe DOM creation for progressively rendered posts.
- `site/src/lib/feed-controller.ts` — history, controls, progressive rendering, captions, and carousels.
- `site/src/components/*.astro` — header, media, post card, caption, and footer markup.
- `site/src/layouts/BaseLayout.astro` — document shell, metadata, and global assets.
- `site/src/pages/index.astro` — first 12 server-rendered rows and embedded remaining data.
- `site/src/styles/global.css` — tokenized responsive single-column presentation.

### Tests and automation

- `tests/unit/` — pure Python behavior and boundary tests.
- `tests/integration/` — complete sync scenarios and real local media transformations.
- `tests/fixtures/build_sample_archive.py` — deterministic sanitized site fixture generator.
- `site/src/**/*.test.ts` — Vitest DOM and state tests.
- `site/tests/feed.spec.ts` — Playwright responsive and interaction scenarios.
- `.github/workflows/test.yml` — offline test/build/browser verification.
- `.github/workflows/sync-and-deploy.yml` — scheduled/manual sync, snapshot replacement, and Pages deployment.

---

### Task 1: Project foundation and versioned archive records

**Files:**

- Create: `.gitignore`
- Create: `pyproject.toml`
- Create: `sync/__init__.py`
- Create: `sync/archive/__init__.py`
- Create: `sync/archive/models.py`
- Create: `sync/archive/validation.py`
- Create: `tests/unit/test_archive_models.py`
- Create: `tests/unit/test_archive_validation.py`

**Interfaces:**

- Produces: `AssetRecord`, `MediaRecord`, `ArchivePost`, `Manifest`, and `SyncState` frozen dataclasses.
- Produces: `load_manifest(data: object) -> Manifest`, `dump_manifest(manifest: Manifest) -> dict[str, object]`, `load_sync_state(data: object) -> SyncState`, `dump_sync_state(state: SyncState) -> dict[str, object]`.
- Produces: `validate_manifest(manifest: Manifest) -> None` and `parse_utc(value: str) -> datetime`.
- `MediaRecord` contains `position`, `kind`, a primary `asset: AssetRecord`, and a required WebP `preview: AssetRecord`; this records hashes for thumbnails and video posters without treating renditions as extra carousel slides.

- [ ] **Step 1: Add test-runner scaffolding without domain behavior**

Create `pyproject.toml` with exact runtime dependencies `instaloader==4.15.3` and `Pillow==12.3.0`; exact development dependencies `pytest==9.1.1`, `pytest-cov==7.1.0`, `ruff==0.16.6`, `mypy==2.3.1`, and `PyYAML==6.0.3`; console script `instagram-saved-archive = "sync.cli:main"`; Ruff line length 100; pytest paths `tests/unit` and `tests/integration`; and strict mypy settings. Add cache, virtualenv, generated site input, Playwright output, session, and media temp patterns to `.gitignore`. Add empty package initializers only.

- [ ] **Step 2: Write failing archive-model tests**

Write tests that dynamically import `sync.archive.models`, load a valid mixed carousel, and assert exact typed values. Add cases for duplicate shortcodes, duplicate media positions, an invalid future schema, a non-UTC timestamp, `../` traversal, absolute paths, invalid SHA-256, a video without duration, and an image with duration.

```python
def test_load_manifest_preserves_order_and_renditions(valid_manifest_dict: dict[str, object]) -> None:
    models = importlib.import_module("sync.archive.models")
    manifest = models.load_manifest(valid_manifest_dict)

    assert [post.shortcode for post in manifest.posts] == ["CAROUSEL1", "IMAGE0001"]
    assert manifest.posts[0].media[1].position == 1
    assert manifest.posts[0].media[1].preview.asset_path.endswith("01-poster.webp")
```

- [ ] **Step 3: Run the tests and confirm the red state**

Run: `python -m pytest tests/unit/test_archive_models.py tests/unit/test_archive_validation.py -q`

Expected: failures because the archive model and validation modules do not exist.

- [ ] **Step 4: Implement strict records and conversion**

Use frozen, slotted dataclasses and enums. Reject unknown keys rather than silently discarding them. Normalize captions with NFC and `\r\n`/`\r` to `\n`, but preserve all other caption content. Sort posts by `archived_at` descending and shortcode ascending as a deterministic tie-breaker.

```python
@dataclass(frozen=True, slots=True)
class AssetRecord:
    asset_path: str
    mime_type: str
    width: int
    height: int
    byte_size: int
    sha256: str

@dataclass(frozen=True, slots=True)
class MediaRecord:
    position: int
    kind: Literal["image", "video"]
    asset: AssetRecord
    preview: AssetRecord
    duration_seconds: float | None = None

@dataclass(frozen=True, slots=True)
class ArchivePost:
    shortcode: str
    creator_username: str
    creator_id: int
    source_url: str
    caption: str
    published_at: datetime
    archived_at: datetime
    verified_at: datetime
    media_type: Literal["image", "video", "carousel"]
    publication_state: Literal["published"]
    unpublished_reason: None
    media: tuple[MediaRecord, ...]
```

- [ ] **Step 5: Verify green and commit**

Run: `python -m pytest tests/unit/test_archive_models.py tests/unit/test_archive_validation.py -q`

Expected: all Task 1 tests pass.

Run: `git add .gitignore pyproject.toml sync tests/unit && git commit -m "feat: define archive data model"`

---

### Task 2: Snapshot store, removals, and publication budgets

**Files:**

- Create: `sync/archive/store.py`
- Create: `sync/archive/removals.py`
- Create: `sync/archive/budget.py`
- Create: `tests/unit/test_archive_store.py`
- Create: `tests/unit/test_removals.py`
- Create: `tests/unit/test_budget.py`

**Interfaces:**

- Consumes: Task 1 archive records and converters.
- Produces: `SnapshotStore(root: Path)`, with `initialize()`, `load()`, `write_atomic(manifest, state)`, `validate_files(manifest)`, and `size_bytes()`.
- Produces: `parse_removals(path: Path) -> frozenset[str]` and `apply_removals(manifest, removals) -> tuple[Manifest, tuple[str, ...]]`.
- Produces: `BudgetStatus(level: Literal["ok", "warning", "reject"], total_bytes: int, largest: tuple[PathSize, ...])` and `check_budget(root: Path) -> BudgetStatus`.

- [ ] **Step 1: Write failing store and removal tests**

Cover empty initialization, deterministic JSON ending in one newline, fsync/replace behavior through observable old-or-new results, on-disk hash validation, missing files, extra unreferenced media, symlink rejection, comments and blanks in removals, removal idempotency, and media-directory deletion lists.

```python
def test_explicit_removal_is_idempotent(sample_manifest: Manifest, tmp_path: Path) -> None:
    removals = tmp_path / "removals.txt"
    removals.write_text("# rights request\nCAROUSEL1\nCAROUSEL1\n", encoding="utf-8")

    once, deleted = apply_removals(sample_manifest, parse_removals(removals))
    twice, deleted_again = apply_removals(once, parse_removals(removals))

    assert [post.shortcode for post in once.posts] == ["IMAGE0001"]
    assert deleted == ("CAROUSEL1",)
    assert twice == once
    assert deleted_again == ()
```

- [ ] **Step 2: Write failing size-boundary tests**

Use a fake size walker injection so exact thresholds do not allocate giant files. Assert `849_999_999 -> ok`, `850_000_000 -> warning`, `899_999_999 -> warning`, `900_000_000 -> reject`, and `95 * 1024 * 1024 + 1 -> reject` for one file.

- [ ] **Step 3: Verify red**

Run: `python -m pytest tests/unit/test_archive_store.py tests/unit/test_removals.py tests/unit/test_budget.py -q`

Expected: failures because store, removal, and budget behavior is absent.

- [ ] **Step 4: Implement atomic snapshot operations**

Write JSON to a named temporary sibling opened with mode `0600`, flush and `os.fsync`, then `os.replace` and fsync the parent directory on POSIX. Validation resolves every asset under `root`, rejects symlinks, ensures recorded sizes and hashes match, and rejects unreferenced files below `media/`. Removal parsing validates Instagram shortcode syntax `[A-Za-z0-9_-]+` and deduplicates entries.

- [ ] **Step 5: Verify green and commit**

Run: `python -m pytest tests/unit/test_archive_store.py tests/unit/test_removals.py tests/unit/test_budget.py -q`

Expected: all Task 2 tests pass.

Run: `git add sync/archive tests/unit && git commit -m "feat: manage atomic archive snapshots"`

---

### Task 3: Instagram boundary, failure classification, and session bootstrap

**Files:**

- Create: `sync/instagram/__init__.py`
- Create: `sync/instagram/models.py`
- Create: `sync/instagram/protocol.py`
- Create: `sync/instagram/errors.py`
- Create: `sync/instagram/retry.py`
- Create: `sync/instagram/client.py`
- Create: `sync/bootstrap_session.py`
- Create: `tests/unit/test_instagram_retry.py`
- Create: `tests/unit/test_instagram_client.py`
- Create: `tests/unit/test_bootstrap_session.py`

**Interfaces:**

- Produces: `InstagramClient` protocol with `validate_identity(username)`, `iter_saved()`, `materialize(candidate)`, `download(source, destination)`, and `verify(shortcode)`.
- Produces: `SavedCandidate(token: object)`, `PublicPost`, `SourceMedia`, and `VerificationStatus` values. A candidate deliberately exposes no printable post metadata until public eligibility is established.
- Produces: `ArchiveError` hierarchy with exit codes: authentication `20`, throttle `21`, transient exhaustion `22`, validation `30`, size `31`, publication `40`.
- Produces: `retry_transport(operation, policy, sleep, random_value)`; only `TransientTransportError` is retried.
- Produces: `bootstrap_session(username: str, loader_factory=Instaloader) -> str` returning base64 text.

- [ ] **Step 1: Write failing retry and classification tests**

Assert capped delays with injected zero jitter, jitter within the cap, success after two transient failures, exhaustion after the configured attempts, and zero retry/sleep for login, checkpoint, challenge, throttle, and unavailable-post errors.

```python
def test_checkpoint_is_never_retried() -> None:
    attempts = 0
    def operation() -> None:
        nonlocal attempts
        attempts += 1
        raise CheckpointError("session refresh required")

    with pytest.raises(CheckpointError):
        retry_transport(operation, RetryPolicy(), sleep=lambda _: None, random_value=lambda: 0.0)
    assert attempts == 1
```

- [ ] **Step 2: Write failing adapter and bootstrap tests**

Use injected fake Instaloader and context objects, not network mocks. Verify `load_session_from_file` and `test_login` identity matching, `Profile.get_saved_posts()` lazy iteration, owner privacy check before `PublicPost` construction, image/video/sidecar ordering, streamed downloads, post verification outcomes, and exception translation. Bootstrap tests verify interactive login, two-factor pass-through, identity mismatch rejection, mode-`0600` temporary storage, base64 output, and deletion after encoding.

- [ ] **Step 3: Verify red**

Run: `python -m pytest tests/unit/test_instagram_retry.py tests/unit/test_instagram_client.py tests/unit/test_bootstrap_session.py -q`

Expected: failures because the Instagram boundary is absent.

- [ ] **Step 4: Implement the protocol and Instaloader adapter**

Map Instaloader's authentication/checkpoint exceptions before generic transport exceptions. Call `load_session_from_file(username, session_path)` and require `test_login() == username`. Use `Profile.from_username(context, username).get_saved_posts()` for discovery, `Post.from_shortcode(context, shortcode)` for reconciliation, `owner_profile.is_private` for eligibility, `post.url`/`post.video_url` for single media, and ordered `get_sidecar_nodes()` for carousels. Stream responses to a destination opened with exclusive creation and never interpolate URLs, response bodies, or session values into exceptions.

- [ ] **Step 5: Verify green and commit**

Run: `python -m pytest tests/unit/test_instagram_retry.py tests/unit/test_instagram_client.py tests/unit/test_bootstrap_session.py -q`

Expected: all Task 3 tests pass without network access.

Run: `git add sync/instagram sync/bootstrap_session.py tests/unit && git commit -m "feat: isolate Instagram authentication and discovery"`

---

### Task 4: Deterministic image and video processing

**Files:**

- Create: `sync/media/__init__.py`
- Create: `sync/media/images.py`
- Create: `sync/media/videos.py`
- Create: `sync/media/processor.py`
- Create: `tests/unit/test_image_processing.py`
- Create: `tests/unit/test_video_commands.py`
- Create: `tests/integration/test_media_processing.py`
- Create: `tests/fixtures/media/.gitkeep`

**Interfaces:**

- Consumes: Task 1 `AssetRecord` and `MediaRecord`; Task 3 `SourceMedia`.
- Produces: `fit_image(width, height, longest_edge) -> Dimensions` and `process_image(source, primary, preview) -> MediaRecord`.
- Produces: `probe_video(path) -> VideoProbe`, `fit_video(width, height) -> Dimensions`, `build_ffmpeg_command(probe, source, output) -> list[str]`, and `process_video(source, primary, poster) -> MediaRecord`.
- Produces: `PostMediaProcessor.process(post, download, temporary_root) -> tuple[MediaRecord, ...]`, which either returns every ordered item or leaves no post directory.

- [ ] **Step 1: Write failing geometry and image tests**

Assert landscape, portrait, square, under-limit, EXIF-rotated, animated/unsupported, malformed, and zero-sized cases. Process generated RGB and EXIF-rotated JPEGs and assert decoded WebP dimensions, metadata absence, quality-independent successful decode, 1600/640 limits, and no upscaling.

```python
@pytest.mark.parametrize(
    ("source", "limit", "expected"),
    [((3200, 1800), 1600, (1600, 900)), ((600, 900), 1600, (600, 900)), ((800, 800), 640, (640, 640))],
)
def test_fit_image_never_upscales(source: tuple[int, int], limit: int, expected: tuple[int, int]) -> None:
    assert fit_image(*source, limit).as_tuple() == expected
```

- [ ] **Step 2: Write failing FFmpeg command and integration tests**

Generate a two-second color/test-tone MP4 and a silent portrait MP4 with FFmpeg inside temporary directories. Assert output bounds, even dimensions, H.264, yuv420p, optional AAC at 128 kbps, CRF 24, `-movflags +faststart`, decoded output, generated WebP poster, hash/size metadata, and rejection over the per-file limit.

- [ ] **Step 3: Write the incomplete-carousel regression test**

Supply three fake downloads where the second raises `TransientTransportError`. Assert the processor raises and neither a final shortcode directory nor any manifest records exist.

- [ ] **Step 4: Verify red**

Run: `python -m pytest tests/unit/test_image_processing.py tests/unit/test_video_commands.py tests/integration/test_media_processing.py -q`

Expected: failures because media processors are absent.

- [ ] **Step 5: Implement processing and post-encode probing**

Apply `ImageOps.exif_transpose`, convert to RGB, use Lanczos downsampling, and save WebP with `quality=82`, `method=6`, and no EXIF/ICC metadata. Build FFmpeg arguments as a list with `-nostdin`, `-hide_banner`, `-loglevel error`, scale/pad-free contained dimensions, `libx264`, `-preset medium`, CRF 24, yuv420p, conditional AAC, and faststart. Run FFprobe JSON parsing with a timeout. Write all items under one temporary shortcode directory and rename it only after every output validates.

- [ ] **Step 6: Verify green and commit**

Run: `python -m pytest tests/unit/test_image_processing.py tests/unit/test_video_commands.py tests/integration/test_media_processing.py -q`

Expected: all Task 4 tests pass and no test contacts Instagram.

Run: `git add sync/media tests/unit tests/integration tests/fixtures && git commit -m "feat: process archive media deterministically"`

---

### Task 5: Transactional sync engine and aggregate reporting

**Files:**

- Create: `sync/engine.py`
- Create: `sync/reporting.py`
- Create: `tests/unit/test_reporting.py`
- Create: `tests/integration/fakes.py`
- Create: `tests/integration/test_sync_discovery.py`
- Create: `tests/integration/test_sync_reconciliation.py`
- Create: `tests/integration/test_sync_failures.py`

**Interfaces:**

- Consumes: `InstagramClient`, `SnapshotStore`, `PostMediaProcessor`, and removal/budget functions.
- Produces: `SyncOptions(max_new_posts: int = 50, full_scan: bool = False, known_threshold: int = 20, reconcile_limit: int = 20)`.
- Produces: `SyncReport(new_count, known_count, private_skip_count, unavailable_skip_count, explicit_removal_count, automatic_removal_count, media_failure_shortcodes, backfill_complete, snapshot_bytes)`.
- Produces: `SyncEngine.run(snapshot: Path, removals: Path, now: datetime, options: SyncOptions) -> SyncReport`.
- Produces: `SecretRedactor(values: Iterable[str]).redact(text: str) -> str` and `render_job_summary(report, failure=None) -> str`.

- [ ] **Step 1: Write failing discovery/backfill tests**

Use an in-memory fake implementing the real protocol. Cover a fresh run limited to 50 eligible posts, scanning past known posts during backfill, completion only at iterator exhaustion, post-backfill stop after 20 consecutive known/removed posts, reset on a new private candidate without retaining its fields, explicit removals before discovery, duplicate repeated runs, and full-scan override.

```python
def test_private_candidate_resets_known_counter_without_persisting_metadata(harness: SyncHarness) -> None:
    harness.client.saved = harness.sequence(known=19) + [harness.private("SECRET1", "private_owner")] + harness.sequence(known=19) + [harness.public_image("NEWPUBLIC")]

    report = harness.run(backfill_complete=True)

    assert report.new_count == 1
    serialized = harness.snapshot_text()
    assert "SECRET1" not in serialized
    assert "private_owner" not in serialized
```

- [ ] **Step 2: Write failing reconciliation tests**

Cover rotating groups of 20, wraparound, fewer than 20 posts, cursor updates after deletion, public timestamp refresh, definitive missing removal, private transition removal, transient verification abort without removal, unsaved-but-public preservation, and explicit removal never re-added.

- [ ] **Step 3: Write failing interruption and redaction tests**

Raise during download, media processing, manifest validation, and budget checking. Assert the original snapshot bytes are identical after each failure. Register cookie, authorization, session, GitHub-token, and arbitrary secret literals with `SecretRedactor`; assert none appear in logs or summaries. Assert private skips are aggregate-only.

- [ ] **Step 4: Verify red**

Run: `python -m pytest tests/unit/test_reporting.py tests/integration/test_sync_discovery.py tests/integration/test_sync_reconciliation.py tests/integration/test_sync_failures.py -q`

Expected: failures because orchestration and reporting are absent.

- [ ] **Step 5: Implement one transaction around the complete run**

Copy the input snapshot to a unique sibling staging directory; apply explicit removals; discover candidates; stage complete posts; reconcile the selected window; update UTC state; validate records, paths, hashes, and budget; atomically replace the caller's plain snapshot directory only on success. Clamp manual limits to 1–50. Never call `repr()` on an Instagram candidate or exception body. Advance the reconciliation cursor only after successful verification of the batch.

- [ ] **Step 6: Verify green and commit**

Run: `python -m pytest tests/unit/test_reporting.py tests/integration/test_sync_discovery.py tests/integration/test_sync_reconciliation.py tests/integration/test_sync_failures.py -q`

Expected: all Task 5 tests pass.

Run: `git add sync/engine.py sync/reporting.py tests && git commit -m "feat: synchronize archive transactionally"`

---

### Task 6: CLI, site export, and safe orphan snapshot publication

**Files:**

- Create: `sync/site_input/__init__.py`
- Create: `sync/site_input/exporter.py`
- Create: `sync/archive/publisher.py`
- Create: `sync/cli.py`
- Create: `tests/unit/test_site_exporter.py`
- Create: `tests/integration/test_cli.py`
- Create: `tests/integration/test_snapshot_publisher.py`

**Interfaces:**

- Produces commands: `init-snapshot`, `validate-snapshot`, `sync`, `prepare-site-input`, `check-budget`, and `publish-snapshot`.
- Produces: `export_site_input(snapshot: Path, destination: Path) -> None`, which copies only validated published records and referenced assets.
- Produces: `publish_snapshot(snapshot: Path, source_repository: Path, expected_repository: str, remote_url: str, env: Mapping[str, str], run=run_command) -> str` returning the root commit SHA.
- `publish-snapshot` always pushes `HEAD:refs/heads/archive-data`; no branch argument exists.

- [ ] **Step 1: Write failing exporter and CLI tests**

Assert clean-destination replacement, no unpublished/unreferenced data, path preservation, strict CLI argument bounds, stable exit codes, JSON report on standard output, human diagnostics on standard error, and no secret/environment dump.

- [ ] **Step 2: Write failing publisher safety tests using real local Git repositories**

Create a temporary source repository plus bare remote. Assert publication creates one root commit containing only `manifest.json`, `sync-state.json`, and `media/`; a second publication force-replaces it with one reachable root; `main` remains unchanged; repository identity mismatch refuses before any Git mutation; a snapshot inside the source worktree refuses; invalid data refuses; a destination symlink refuses; and command construction contains only literal `archive-data`.

```python
def test_publisher_never_changes_main(local_remote: GitHarness, valid_snapshot: Path) -> None:
    main_before = local_remote.rev_parse("refs/heads/main")
    publish_snapshot(valid_snapshot, local_remote.source, "owner/archive", local_remote.url, local_remote.github_env)

    assert local_remote.rev_parse("refs/heads/main") == main_before
    assert local_remote.parent_count("refs/heads/archive-data") == 0
```

- [ ] **Step 3: Verify red**

Run: `python -m pytest tests/unit/test_site_exporter.py tests/integration/test_cli.py tests/integration/test_snapshot_publisher.py -q`

Expected: failures because commands and publication safeguards are absent.

- [ ] **Step 4: Implement the CLI and publisher**

Build `argparse` subcommands with `Path.resolve(strict=True)` checks. The publisher verifies `GITHUB_REPOSITORY == expected_repository`, parses the source remote with HTTPS/SSH normalization, rejects paths at or inside the source worktree, validates the snapshot, creates a separate temporary Git repository, copies snapshot contents without symlinks, commits with deterministic author metadata, adds the supplied matching remote, and runs exactly `git push --force origin HEAD:refs/heads/archive-data`.

- [ ] **Step 5: Verify green and commit**

Run: `python -m pytest tests/unit/test_site_exporter.py tests/integration/test_cli.py tests/integration/test_snapshot_publisher.py -q`

Expected: all Task 6 tests pass.

Run: `git add sync tests && git commit -m "feat: expose safe archive operations"`

---

### Task 7: Astro build foundation and server-rendered feed

**Files:**

- Create: `site/package.json`
- Create: `site/package-lock.json`
- Create: `site/astro.config.mjs`
- Create: `site/tsconfig.json`
- Create: `site/vitest.config.ts`
- Create: `site/src/env.d.ts`
- Create: `site/src/data/archive.ts`
- Create: `site/src/data/archive.test.ts`
- Create: `site/src/layouts/BaseLayout.astro`
- Create: `site/src/components/ArchiveHeader.astro`
- Create: `site/src/components/MediaGallery.astro`
- Create: `site/src/components/Caption.astro`
- Create: `site/src/components/PostCard.astro`
- Create: `site/src/components/ArchiveFooter.astro`
- Create: `site/src/pages/index.astro`
- Create: `site/src/styles/global.css`
- Create: `tests/fixtures/build_sample_archive.py`

**Interfaces:**

- Consumes: validated `site/public/archive/manifest.json` and `sync-state.json` prepared by Task 6.
- Produces: `loadArchive(root?: string) -> ArchiveData` with the same field names as Python JSON.
- Produces: `scriptSafeJson(value: unknown) -> string`, escaping `<`, `>`, `&`, U+2028, and U+2029.
- Produces: accessible first-page HTML with at most 12 `.post-row` articles.

- [ ] **Step 1: Add exact site tooling and deterministic fixture generation**

Set `type: module`; pin `astro==7.3.1`; pin development dependencies `@astrojs/check==0.9.10`, `@playwright/test==1.63.0`, `@types/node==26.4.1`, `jsdom==30.0.1`, `prettier==3.9.6`, `prettier-plugin-astro==0.14.1`, `typescript==6.0.3`, and `vitest==5.0.0`. Scripts are `check`, `test`, `test:e2e`, `build`, and `format:check`. Generate and commit `package-lock.json` with `npm install --package-lock-only`.

The fixture builder creates 15 public records spanning image, video, and mixed carousel types, captions with Unicode and `</script>`, small local WebP/MP4/poster assets, correct hashes, and a valid sync state under a requested output directory.

- [ ] **Step 2: Write failing archive loader tests**

Assert strict schema/version handling, missing local asset failure, URL/path mapping beneath `/archive/`, manifest ordering, configurable title default `Saved`, and safe JSON that cannot close a script element.

```typescript
it('neutralizes script-closing captions', () => {
  const output = scriptSafeJson({ caption: '</script><script>alert(1)</script>' });
  expect(output).not.toContain('</script>');
  expect(output).toContain('\\u003c/script');
});
```

- [ ] **Step 3: Verify red**

Run: `cd site && npm ci && npm test -- src/data/archive.test.ts`

Expected: failure because archive loading does not exist.

- [ ] **Step 4: Implement loader, semantic components, and visual system**

Use CSS custom properties for warm off-white canvas, white cards, ink/muted text, one restrained blue accent, 14px card radius, subtle border/shadow, and a system sans-serif stack. The sticky header uses a translucent backdrop; the feed is `display:grid; grid-template-columns:minmax(0, 1fr)` with `max-width:760px`; media uses `width:100%`, `height:auto`, and `max-height:min(82vh, 960px)` with `object-fit:contain`. Render creators as external links with safe `rel`, use `<time datetime>`, native video controls/preload metadata/poster, lazy image loading/async decode/srcset, accessible carousel labels, and caption buttons.

- [ ] **Step 5: Build against the fixture and verify green**

Run:

```bash
python tests/fixtures/build_sample_archive.py --output .tmp/sample-archive
python -m sync.cli prepare-site-input --snapshot .tmp/sample-archive --destination site/public/archive
cd site && npm test -- src/data/archive.test.ts && npm run check && npm run build
```

Expected: loader tests and Astro check pass; `site/dist/index.html` exists with 12 `.post-row` articles and no literal `</script><script>alert` string.

- [ ] **Step 6: Commit**

Run: `git add site tests/fixtures && git commit -m "feat: render static archive feed"`

---

### Task 8: Search, filters, sorting, progressive rendering, captions, and carousel controls

**Files:**

- Create: `site/src/lib/feed-state.ts`
- Create: `site/src/lib/feed-state.test.ts`
- Create: `site/src/lib/render-post.ts`
- Create: `site/src/lib/render-post.test.ts`
- Create: `site/src/lib/feed-controller.ts`
- Create: `site/src/lib/feed-controller.test.ts`
- Modify: `site/src/pages/index.astro`
- Modify: `site/src/components/ArchiveHeader.astro`
- Modify: `site/src/components/MediaGallery.astro`
- Modify: `site/src/components/Caption.astro`
- Modify: `site/src/styles/global.css`

**Interfaces:**

- Produces: `parseFeedState(search: string) -> FeedState`, `serializeFeedState(state) -> string`, and `selectPosts(posts, state) -> ArchivePost[]`.
- Produces: `renderPost(post, document) -> HTMLElement` using DOM APIs and text nodes only.
- Produces: `mountFeed(root: HTMLElement, posts: ArchivePost[], window: Window) -> () => void`, returning cleanup.

- [ ] **Step 1: Write failing pure state tests**

Cover case-insensitive Unicode search across username/caption, exact media filters, archive/original date ordering with shortcode tie-breaks, omitted default query values, encoded search values, invalid-value fallback, and stable round-trip parsing.

- [ ] **Step 2: Write failing DOM/controller tests**

In jsdom, cover the 12-item initial bound, batch reveal after sentinel intersection, no network calls, XSS captions rendered as text, caption `aria-expanded`, empty results, control-to-URL updates via `history.pushState`, `popstate` restoration, focus preservation, carousel buttons, left/right keyboard handling only while focused within the carousel, pointer swipe threshold, position status, and reduced-motion scroll behavior.

```typescript
it('restores filters on browser navigation', () => {
  const mounted = setupFeed('?q=mountain&type=image&sort=published');
  window.history.pushState({}, '', '?type=video');
  window.dispatchEvent(new PopStateEvent('popstate'));

  expect(mounted.typeSelect.value).toBe('video');
  expect(mounted.visibleShortcodes()).toEqual(['VIDEO0001']);
});
```

- [ ] **Step 3: Verify red**

Run: `cd site && npm test -- src/lib`

Expected: failures because state, rendering, and controller modules are absent.

- [ ] **Step 4: Implement framework-free interaction modules**

Keep state functions pure. Build later rows with `createElement`, `textContent`, validated local paths, and fixed safe attributes; never use caption/username HTML. Use event delegation at the feed root, `IntersectionObserver` for 12-row batches, `pushState` for user control changes, `popstate` for restoration, pointer capture for horizontal swipe, and `matchMedia('(prefers-reduced-motion: reduce)')` to disable smooth behavior.

- [ ] **Step 5: Verify green and commit**

Run: `cd site && npm test -- src/lib && npm run check && npm run format:check`

Expected: all Task 8 unit tests pass with no type or format errors.

Run: `git add site/src && git commit -m "feat: add archive feed interactions"`

---

### Task 9: Browser-level responsive, accessibility, and static-output checks

**Files:**

- Create: `site/playwright.config.ts`
- Create: `site/tests/feed.spec.ts`
- Create: `site/scripts/check-static-output.mjs`
- Modify: `site/package.json`
- Modify: `site/package-lock.json`

**Interfaces:**

- Produces: Playwright coverage on Chromium at 375×812, 768×1024, and 1440×1000.
- Produces: `npm run check:static`, which rejects missing referenced local assets, remote media sources, unexpected files, and artifacts at or above 900 MB.

- [ ] **Step 1: Write browser scenarios and run them red**

Test exactly one card in every horizontal row, feed width at most 760px, document scroll width equal to viewport width, contained media, native video controls/no autoplay, attribution links, all filters/sorts/search, reload and back/forward query restoration, caption keyboard control, carousel click/keyboard/swipe, lazy image attributes, and progressive increase from 12 to 15 cards.

Run: `cd site && npx playwright install chromium && npm run test:e2e`

Expected: failures because Playwright configuration and any browser-only gaps are not implemented.

- [ ] **Step 2: Write and run static-output tests red**

The checker parses built HTML for `/archive/...` references, verifies every reference beneath `dist`, rejects `instagram` CDN media URLs, scans symlinks and files, and calls the exact aggregate/per-file budget logic through a CLI JSON result.

Run: `cd site && npm run check:static`

Expected: failure until the checker and scripts are wired.

- [ ] **Step 3: Implement configuration and close observed browser gaps**

Configure one local Astro preview server, retries only in CI, traces/screenshots on first retry, the three named viewports, and no external network. Adjust only markup/styles/controller behavior demonstrated by the failing scenarios.

- [ ] **Step 4: Verify green and commit**

Run:

```bash
cd site
npm run check
npm test
npm run build
npm run check:static
npm run test:e2e
```

Expected: type checks, unit tests, production build, static checks, and all three browser projects pass.

Run: `git add site && git commit -m "test: verify archive site behavior"`

---

### Task 10: Offline CI workflow

**Files:**

- Create: `.github/workflows/test.yml`
- Create: `tests/unit/test_workflow_contracts.py`

**Interfaces:**

- Produces: pull-request and `main` push verification with no Instagram secrets or network calls from tests.

- [ ] **Step 1: Write failing workflow-contract tests**

Parse YAML with `yaml.safe_load`. Assert PR and main-push triggers, `permissions: contents: read`, Python 3.12, Node 24, exact current action majors (`checkout@v7`, `setup-python@v7`, `setup-node@v7`), FFmpeg presence check, editable dev install, Ruff format/lint, mypy, full pytest, fixture generation, snapshot validation/export, `npm ci`, Astro check/tests/build/static check, Playwright Chromium installation, and browser tests. Assert no `INSTAGRAM_SESSION_B64` or write permissions appear.

- [ ] **Step 2: Verify red**

Run: `python -m pytest tests/unit/test_workflow_contracts.py -q`

Expected: failure because `test.yml` does not exist.

- [ ] **Step 3: Implement the workflow**

Use one Ubuntu job with timeouts and dependency caching keyed by `pyproject.toml` and `site/package-lock.json`. Use `python -m` entry points, never print environments, and upload Playwright diagnostics only on failure with a short retention.

- [ ] **Step 4: Verify green and commit**

Run: `python -m pytest tests/unit/test_workflow_contracts.py -q && python -c 'import yaml; yaml.safe_load(open(".github/workflows/test.yml"))'`

Expected: workflow contract tests pass and YAML parses.

Run: `git add .github/workflows/test.yml tests/unit/test_workflow_contracts.py && git commit -m "ci: verify archive and static site"`

---

### Task 11: Scheduled sync and GitHub Pages deployment workflow

**Files:**

- Create: `.github/workflows/sync-and-deploy.yml`
- Modify: `tests/unit/test_workflow_contracts.py`

**Interfaces:**

- Produces: daily `17 10 * * *` UTC and manual `full_scan`/`max_new_posts` execution.
- Consumes: `INSTAGRAM_USERNAME` Actions variable and masked `INSTAGRAM_SESSION_B64` secret.
- Publishes: hardcoded `archive-data` snapshot and GitHub Pages artifact.

- [ ] **Step 1: Add failing security and ordering contract tests**

Assert `concurrency.cancel-in-progress: false`; permissions exactly `contents: write`, `pages: write`, `id-token: write`; environment `github-pages`; no password inputs; session decode under `$RUNNER_TEMP`; mode 600; cleanup trap; identity validation before saved-feed access; archive fetch/init before sync; all validate/test/build/static steps before publish; publish before Pages upload/deploy; `configure-pages@v6`, `upload-pages-artifact@v5`, `deploy-pages@v5`; no shell tracing; and literal `archive-data` only.

- [ ] **Step 2: Verify red**

Run: `python -m pytest tests/unit/test_workflow_contracts.py -q`

Expected: failure because the deployment workflow is absent.

- [ ] **Step 3: Implement the transactional workflow**

Check out the triggering `main` revision. Fetch `archive-data` when present and export it with `git archive`; otherwise initialize through the CLI. Decode with `base64 --decode` to `$RUNNER_TEMP/instagram.session`, immediately `chmod 600`, mask the username, and install an `EXIT` trap. Invoke sync with serialized Instagram access, prepare site input, run the entire Python/site/static suite, call `publish-snapshot`, then configure/upload/deploy Pages. Append only `SecretRedactor` output to `$GITHUB_STEP_SUMMARY`. Set timeouts on the job and network step.

- [ ] **Step 4: Verify green and commit**

Run:

```bash
python -m pytest tests/unit/test_workflow_contracts.py -q
python -c 'import yaml; yaml.safe_load(open(".github/workflows/sync-and-deploy.yml"))'
rg -n 'set -x|password|env$|printenv|HEAD:refs/heads/main' .github/workflows/sync-and-deploy.yml
```

Expected: tests and YAML parsing pass; the final `rg` command prints nothing and exits 1.

Run: `git add .github/workflows/sync-and-deploy.yml tests/unit/test_workflow_contracts.py && git commit -m "ci: sync archive and deploy Pages"`

---

### Task 12: Owner documentation and complete acceptance verification

**Files:**

- Create: `README.md`
- Create: `config/removals.txt`
- Create: `LICENSE`
- Modify: `pyproject.toml`
- Modify: `site/package.json`

**Interfaces:**

- Produces: copy-paste owner bootstrap, GitHub setup, one-post validation, normal operation, removal, recovery, rights, and limitations instructions.

- [ ] **Step 1: Write a failing documentation contract test**

Create `tests/unit/test_documentation.py` asserting the README includes repository/Pages setup, `INSTAGRAM_USERNAME`, `INSTAGRAM_SESSION_B64`, local `bootstrap-session`, secret replacement, one-post manual run, backfill batches, removal-list syntax, expired-session recovery, size limits, public-rights warning, no private-post archiving, and archive-data/non-erasure caveats.

- [ ] **Step 2: Verify red**

Run: `python -m pytest tests/unit/test_documentation.py -q`

Expected: failure because owner documentation is absent.

- [ ] **Step 3: Write the README, empty canonical removals file, and license**

Document supported local versions (Python 3.12, Node 24, FFmpeg 6+), `python -m venv`, editable dev install, site install, test commands, `bootstrap-session --username`, safe transfer of standard output directly into GitHub Secrets, and cleanup. Explain that the site is public, public-source status is not publication permission, GitHub/Instagram limitations apply, and the MIT license covers repository code rather than archived creator media.

- [ ] **Step 4: Run the full clean verification suite**

Run from the repository root:

```bash
python -m ruff format --check .
python -m ruff check .
python -m mypy sync
python -m pytest -q
python tests/fixtures/build_sample_archive.py --output .tmp/sample-archive
python -m sync.cli validate-snapshot --snapshot .tmp/sample-archive
python -m sync.cli prepare-site-input --snapshot .tmp/sample-archive --destination site/public/archive
cd site
npm ci
npm run format:check
npm run check
npm test
npm run build
npm run check:static
npm run test:e2e
```

Expected: every command exits 0; pytest/Vitest/Playwright report zero failures; Astro emits `dist`; static checks report an artifact below 850 MB and no missing assets.

- [ ] **Step 5: Inspect the generated site at all target viewports**

Open the locally built site or Playwright screenshots at 375×812, 768×1024, and 1440×1000. Confirm the header remains usable, every row is single-column, media is uncropped, text is readable, controls have visible focus, carousels expose position, and there is no horizontal overflow.

- [ ] **Step 6: Recheck security before committing**

Run:

```bash
git grep -n -I -E 'sessionid|csrftoken|INSTAGRAM_SESSION_B64=' -- ':!docs/superpowers/*' ':!README.md' || true
find . -type f -size +95M -not -path './.git/*' -print
git diff --check
```

Expected: no credential values, oversized files, or whitespace errors.

- [ ] **Step 7: Commit the owner handoff**

Run: `git add README.md LICENSE config/removals.txt pyproject.toml site/package.json site/package-lock.json tests/unit/test_documentation.py && git commit -m "docs: add archive setup and operations guide"`

- [ ] **Step 8: Confirm the final repository state**

Run:

```bash
git status --short
git log --oneline --decorate -15
```

Expected: the worktree is clean and the history contains the independently verified implementation commits from Tasks 1–12.

---

## Acceptance-criteria traceability

| Acceptance criterion | Primary evidence |
|---|---|
| Bootstrap without committed credentials | Tasks 3, 11, 12 bootstrap/session tests and secret scan |
| One-post download/process/archive/build/deploy path | Tasks 4–7 and 11 workflow ordering contract |
| Repeated sync is duplicate-free | Task 5 integration test |
| Private posts leave no durable post data | Tasks 3 and 5 privacy regression tests |
| Unsaving does not remove an archive item | Task 5 reconciliation test |
| Deleted/private sources leave the current snapshot | Task 5 reconciliation and file-removal tests |
| Image/video/carousel single-column rendering | Tasks 7–9 site and browser tests |
| Search/filter/sort/caption/carousel/link behavior | Tasks 8–9 unit and browser tests |
| Failed sync preserves prior durable state | Tasks 5, 6, and 11 interruption/order tests |
| 900 MB and 95 MiB refusal thresholds | Tasks 2, 4, 6, and 9 boundary/static tests |
| No secrets in tests, artifacts, or logs | Tasks 3, 5, 10–12 redaction and repository scans |
