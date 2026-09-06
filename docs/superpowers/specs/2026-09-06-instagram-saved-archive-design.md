# Instagram Saved Archive: GitHub-Only Design

**Date:** 2026-09-06  
**Status:** Approved for implementation

## Summary

Build a public static website that automatically archives Instagram posts saved by the site owner. A scheduled GitHub Actions workflow uses Instaloader to discover saved posts, downloads eligible media, optimizes it for the web, updates a durable archive snapshot, builds the site, and publishes it with GitHub Pages.

The system uses GitHub exclusively. It has no application server, database, object store, Cloudflare service, or third-party scheduler.

## Goals

- Automatically discover and archive saved Instagram posts once per day.
- Rehost media only when the source account is public at verification time.
- Publish a polished public feed with one complete post per row at every viewport size.
- Preserve archived posts when the owner later unsaves them.
- Remove a post from the current published snapshot when its source disappears or its creator becomes private.
- Keep Instagram credentials and session cookies out of source control, generated pages, deployment artifacts, and logs.
- Remain safely below GitHub Pages' 1 GB published-site limit.
- Support fewer than 200 saved posts with images, videos, and carousels.
- Make every workflow idempotent and safely recoverable after interruption.

## Non-goals

- Reproducing Instagram Collections.
- Archiving posts from private accounts.
- Copying comments, likes, view counts, or follower counts.
- Providing visitor accounts, comments, likes, or other social features.
- Providing a browser-based administrative backend.
- Guaranteeing permanent preservation of content removed by its creator.
- Bypassing Instagram authentication challenges or access controls.

## Constraints and operating assumptions

- The archive contains fewer than 200 saved posts at launch.
- The generated GitHub Pages artifact must remain below 900 MB, leaving operational headroom below GitHub's 1 GB limit.
- No individual generated file may exceed 95 MiB, leaving headroom below GitHub's 100 MiB repository-file limit.
- The site is public and read-only.
- The repository may be public or private, subject to the owner's GitHub plan and Pages configuration. The deployed site and media are public in either case.
- Instaloader is an unofficial client. Instagram may invalidate the session, challenge a GitHub-hosted runner, change its private interfaces, or rate-limit access.
- A public Instagram account does not establish permission to republish its media. The owner is responsible for publication rights and can remove any item through the archive manifest workflow.

## Architecture

The repository has two long-lived branches with separate responsibilities:

- `main` contains source code, tests, configuration, the canonical removal list, and GitHub Actions workflows.
- `archive-data` contains only the current media snapshot, archive manifest, and sync state.

The scheduled workflow checks out `main` and materializes `archive-data` into a separate working directory. It downloads and processes new posts, reconciles a bounded set of old posts, generates the static website, replaces `archive-data` with a single orphan snapshot commit, and deploys the built site through GitHub Pages' Actions-based deployment.

The Pages deployment artifact is generated output and is never committed to `main`.

### Implementation boundaries

The Python application is divided into focused modules:

- `sync.archive` owns versioned manifest and sync-state parsing, validation, upgrades, deterministic ordering, size accounting, and transactional snapshot changes.
- `sync.instagram` owns the Instaloader adapter, authenticated identity validation, saved-feed enumeration, source verification, failure classification, and serialized retry behavior. The core sync engine depends on protocols rather than Instaloader objects so tests never contact Instagram.
- `sync.media` owns image and video processing, post-encode validation, content hashing, and atomic media-directory publication.
- `sync.site_input` validates and copies a snapshot into the Astro build input without exposing unpublished records.
- `sync.cli` composes the adapters and exposes commands for sync, snapshot validation, site-input preparation, and orphan-branch safety checks.
- `sync.bootstrap_session` is the only interactive authentication path and emits a base64 session value to standard output without writing credentials into the repository.

The Astro application renders accessible HTML for an initial bounded feed segment and uses small client-side TypeScript modules for filtering, sorting, query-string state, caption disclosure, carousel interaction, and progressive row rendering. It requires no UI framework or server runtime.

### Repository layout

```text
instagram-saved-archive/
├── .github/
│   └── workflows/
│       ├── sync-and-deploy.yml
│       └── test.yml
├── config/
│   └── removals.txt
├── docs/
│   └── superpowers/
│       ├── plans/
│       └── specs/
├── sync/
│   ├── archive/
│   ├── instagram/
│   ├── media/
│   ├── site_input/
│   ├── bootstrap_session.py
│   └── cli.py
├── site/
│   ├── public/
│   ├── src/
│   ├── astro.config.mjs
│   └── package.json
├── tests/
│   ├── fixtures/
│   ├── integration/
│   └── unit/
├── pyproject.toml
└── README.md
```

The `archive-data` branch snapshot has this shape:

```text
archive-data/
├── manifest.json
├── sync-state.json
└── media/
    └── <shortcode>/
        ├── 00.webp
        ├── 00-thumb.webp
        ├── 01.mp4
        └── 01-poster.webp
```

## Archive data model

`manifest.json` is a UTF-8, deterministically formatted JSON document with schema version `1` and an ordered `posts` array. Every post contains:

- Instagram shortcode as the stable primary identifier.
- Creator username and Instagram numeric ID.
- Original post URL.
- Caption text, normalized for line endings and Unicode without rewriting its content.
- Original Instagram publication timestamp in UTC ISO 8601 form.
- First archive discovery timestamp in UTC ISO 8601 form.
- Last successful public-source verification timestamp in UTC ISO 8601 form.
- Media type: `image`, `video`, or `carousel`.
- Ordered media records containing local POSIX path, MIME type, width, height, optional duration, byte size, and lowercase SHA-256 digest.
- Publication state and a machine-readable reason when unpublished.

The public snapshot contains only published records. Transient unpublished candidates exist only in the workflow's temporary directory and private job summary. The manifest never records metadata about skipped private posts. Sync logs report only aggregate private-skip counts.

`sync-state.json` has schema version `1` and contains:

- Whether the initial backfill is complete.
- Last successful sync timestamp.
- Last complete saved-feed scan timestamp.
- Rotating reconciliation cursor.
- The configured consecutive-known-post threshold.
- Current archive byte size.

`config/removals.txt` on `main` contains one explicitly removed shortcode per line. Blank lines and lines beginning with `#` are ignored. A removed shortcode is never automatically re-added unless the owner deletes it from this file.

Schema upgrades are pure transformations. The loader rejects unknown future schema versions, invalid paths, duplicate shortcodes, duplicate media paths, inconsistent media type records, invalid timestamps, bad digests, and paths escaping the snapshot root.

## Instagram authentication

`sync/bootstrap_session.py` performs the only interactive authentication flow. It runs locally, invokes Instaloader's interactive login and two-factor flow, validates that the authenticated profile matches the requested username, and prints the base64-encoded session file for GitHub Actions.

The owner stores the encoded value in the `INSTAGRAM_SESSION_B64` GitHub Actions secret and the username in the `INSTAGRAM_USERNAME` Actions variable. Neither value is written to the repository.

At workflow start, the session secret is decoded into a mode-`0600` file inside the runner's temporary directory. The workflow validates the authenticated identity before reading saved posts. A shell cleanup trap deletes the file when the job completes.

The workflow does not attempt password login. If the session expires or Instagram presents a checkpoint or challenge, the run stops without retrying authentication. The job summary explains how to rerun the bootstrap command and replace the secret.

Automatic persistence of refreshed session data is excluded. This avoids storing account credentials on a public branch or granting the workflow permission to rewrite repository secrets.

## Discovery and backfill

The sync process obtains saved posts from the authenticated owner's profile through the Instagram adapter. All Instagram operations are serialized.

During initial backfill:

- Scan the saved feed from the beginning.
- Skip existing manifest entries without redownloading media.
- Download at most 50 new eligible posts per workflow run, configurable downward or upward for a manual run but bounded to the range 1–50.
- Continue past known posts until the batch limit is reached or the saved feed ends.
- Set `backfill_complete` only after reaching the end of a successful saved-feed enumeration.
- Permit additional batches through `workflow_dispatch`.

After backfill:

- Begin each daily scan at the start of the saved feed.
- Stop after 20 consecutive already archived or explicitly removed posts.
- Reset the consecutive-known counter when encountering any new post, including an ineligible private post, while persisting no post-level data for that private post.
- Permit a manual full saved-feed scan for recovery and reconciliation.

Retriable transport operations use capped exponential backoff with random jitter. Rate-limit, checkpoint, login, and challenge responses stop Instagram processing immediately. A failure before snapshot validation leaves both durable branches and the deployed site unchanged.

## Publication eligibility and atomic ingestion

A post is eligible only when all of the following are true:

- It is visible to the authenticated owner.
- Its creator profile reports that it is public.
- It contains at least one downloadable media item.
- Its shortcode is absent from `config/removals.txt`.
- Every downloaded media item passes validation and processing.

Private-account posts are skipped without saving media, captions, usernames, shortcodes, source URLs, or post-level diagnostic data in the archive snapshot.

Each new post is staged under a unique temporary directory. The process downloads and transforms all ordered media items, probes every result, computes hashes and metadata, validates the completed post record, then atomically renames the completed directory into the snapshot and updates the in-memory manifest. A failed image, carousel item, or video never produces a partially published post.

The sync writes `manifest.json` and `sync-state.json` to temporary sibling files, fsyncs them, and atomically replaces their destinations only after full snapshot validation succeeds.

## Reconciliation and removal

Unsaving an Instagram post does not remove it from the archive.

Every daily run verifies up to 20 published posts, beginning at the stored rotating reconciliation cursor. Verification checks whether the post exists and whether its creator is public. Under the expected 200-post maximum, the complete archive is revisited at least every ten successful runs.

When a source is deleted, unavailable, or newly private:

- Remove the post from the public manifest.
- Remove its media directory from the new snapshot.
- Write only an aggregate automatic-removal count to the job summary.
- Retain no creator or post details after automatic removal.

Every run applies `config/removals.txt` before discovery and reconciliation. Explicitly removed media and manifest entries are deleted from the snapshot. The shortcode remains only in `config/removals.txt`.

The static GitHub design cannot guarantee immediate purging from GitHub caches or unreachable Git objects. Squashing the snapshot prevents ordinary branch history from retaining removed media and reduces repository growth, but it is not a legal-erasure mechanism.

## Media processing

Source downloads and generated assets remain in a temporary working tree until the containing post is complete.

### Images

- Apply EXIF orientation before processing.
- Preserve aspect ratio and never upscale.
- Limit the longest edge to 1600 pixels.
- Encode as WebP at quality 82 with metadata stripped.
- Generate a 640-pixel longest-edge WebP thumbnail.

### Videos

- Preserve aspect ratio and never upscale.
- Fit landscape output within 1280×720 and portrait output within 720×1280.
- Encode H.264 at CRF 24 with a preset selected for GitHub Actions runtime and `yuv420p` pixel format.
- Encode AAC audio at 128 kbps when audio exists and omit an audio track otherwise.
- Place MP4 metadata at the beginning of the file for progressive playback.
- Generate a WebP poster frame within a 640-pixel longest edge.

Every output is decoded or probed after encoding. Validation rejects zero-byte files, malformed media, unsupported formats, wrong dimensions, path traversal, hash or byte-size mismatches, and any file larger than 95 MiB.

Before branch publication, the workflow calculates both the complete snapshot size and built Pages artifact size. At 850 MB it emits a warning in the job summary. At 900 MB or more it refuses to update `archive-data` or deploy the larger artifact. It reports the largest media contributors only in the private job summary.

## Public site experience

The site is a static Astro application built entirely in GitHub Actions. The homepage opens directly to the archive feed; there is no marketing hero or intermediary landing page.

### Feed behavior

- Render exactly one complete post per row at every viewport width.
- Center the feed in a column with a maximum width of approximately 760 pixels.
- Preserve natural media aspect ratios without cropping.
- Cap tall media presentation to the viewport using contained media.
- Use native video controls and never autoplay with sound.
- Give carousels touch/pointer swiping, previous/next buttons, left/right keyboard navigation, position indicators, and reduced-motion support.
- Keep creator attribution, original link, publication date, archive date, and caption visually associated with the media.
- Collapse long captions behind an accessible expand/collapse control.
- Use native lazy loading for images.
- Server-render the first 12 posts and progressively reveal further rows in batches of 12 without network requests.

The sticky header contains:

- A configurable site title, defaulting to `Saved`.
- Text search across creator username and caption.
- A media-type filter for all, images, videos, and carousels.
- A sort control for archive discovery order or original publication date.

Filtering and sorting happen client-side against the generated manifest. Query parameters `q`, `type`, and `sort` represent non-default state. History navigation restores the view without a reload, invalid values fall back to defaults, and controls remain usable without horizontal scrolling.

The footer shows the last successful sync time, current archive item count, source-attribution language, and a statement that rights remain with the original creators.

## GitHub Actions

### Sync and deploy

`.github/workflows/sync-and-deploy.yml` provides:

- Daily cron `17 10 * * *` UTC.
- `workflow_dispatch` inputs `full_scan` (boolean) and `max_new_posts` (integer-like choice from 1 to 50, default 50).
- A repository-scoped concurrency group that allows one archive mutation at a time and does not cancel an in-progress run.
- Minimal job permissions: `contents: write`, `pages: write`, and `id-token: write`.

The job:

1. Checks out `main` at the triggering revision.
2. Fetches and materializes `archive-data` into a separate directory, or initializes an empty valid snapshot.
3. Installs pinned Python, Node, and site dependencies plus FFmpeg.
4. Decodes the session into the runner temporary directory and validates authenticated identity.
5. Runs discovery, bounded backfill, explicit removals, and rotating reconciliation.
6. Processes and validates new media.
7. Validates the manifest, paths, hashes, and size limits.
8. Builds Astro using a prepared read-only snapshot input.
9. Runs static-output and local-asset checks.
10. Replaces `archive-data` with one new orphan snapshot commit and force-pushes only that literal branch.
11. Uploads and deploys the Pages artifact.
12. Writes a redacted job summary.

If steps 3–9 fail, neither the archive branch nor deployment changes. If the archive branch update succeeds but Pages deployment fails, the next run rebuilds from the latest valid snapshot and retries deployment.

Before the force-push helper invokes Git, it verifies the literal branch name `archive-data`, the expected GitHub repository identity, a snapshot directory outside the `main` worktree, both schema documents, all hashes and asset paths, and size constraints. It never accepts a configurable destination branch and never force-pushes `main`.

### Continuous tests

`.github/workflows/test.yml` runs on pull requests and pushes to `main`. It runs Python unit and integration tests, Python formatting and static analysis, fixture and schema validation, site unit tests, a production build using the deterministic sample snapshot, and browser tests for supported interactions and responsive layout. Tests never contact Instagram.

## Error handling, retry, and observability

Failures use stable categories and exit codes:

- Authentication, login, checkpoint, or challenge failure: stop immediately and request local session bootstrap.
- Instagram throttling: stop immediately and wait for a scheduled or manual rerun.
- Transient transport failure: retry with bounded exponential backoff and jitter, then stop if exhausted.
- Individual unavailable discovery candidate: increment an aggregate skip and continue.
- Reconciliation reports a definitively unavailable post: remove it from the staged snapshot.
- Media validation or encoding failure: leave the candidate unpublished, identify its shortcode only in the private job summary, and continue when the adapter confirms it is safe.
- Manifest or snapshot validation failure: abort before changing the branch or deployment.
- Pages size-limit failure: abort publication and report the largest contributors in the private summary.
- Git or deployment failure: preserve the last valid deployment and leave a rerunnable job.

Sensitive values are represented by a `SecretRedactor` that replaces registered literal values and common cookie/header/session patterns before application messages reach logs or summaries. Commands never enable shell tracing. Logs never print the session file, session-cookie values, authorization headers, GitHub tokens, full authenticated Instagram response bodies, or environment dumps.

## Testing strategy

Unit tests cover:

- Manifest parsing, validation, deterministic ordering, and schema upgrades.
- Eligibility decisions for public and private creators.
- Consecutive-known stopping behavior.
- Backfill completion and batch limits.
- Removal-list behavior.
- Reconciliation cursor rotation.
- Media path construction and content hashing.
- Image sizing and non-upscaling calculations.
- Video fit calculations and FFmpeg command generation with and without audio.
- Size-budget warning and refusal thresholds.
- Secret and log redaction.
- Failure classification and retry decisions.

Integration tests use recorded sanitized JSON fixtures and generated local media samples to cover:

- A new image post.
- A video with and without audio.
- A mixed-media carousel.
- Duplicate ingestion across repeated runs.
- An interrupted download.
- An incomplete carousel.
- A creator transition from public to private.
- A deleted post.
- An explicitly removed shortcode.
- A simulated expired session and rate-limit response.
- Snapshot generation and orphan-branch safety checks.

Site tests verify:

- One-post-per-row layout at all CSS breakpoints.
- No unintended horizontal scrolling.
- Carousel pointer and keyboard interactions.
- Caption expansion.
- Client-side search, filtering, sorting, and URL-state restoration.
- Lazy-loading behavior and progressive feed rendering.
- Correct attribution and original links.
- Successful static build with no missing local assets.

The first real-account verification is a manually triggered production workflow limited to one new post. The daily schedule is retained only after that run downloads, builds, and deploys successfully.

## Setup and operation

The README guides the owner through:

1. Creating the GitHub repository and pushing `main`.
2. Enabling GitHub Pages with GitHub Actions as the deployment source.
3. Configuring `INSTAGRAM_USERNAME` as an Actions variable.
4. Running the local bootstrap command and saving its encoded output as `INSTAGRAM_SESSION_B64`.
5. Running the test workflow.
6. Manually running a one-post sync.
7. Confirming the deployed result.
8. Retaining the daily cron schedule.
9. Running additional manual backfill batches when desired.

Normal administration happens through GitHub Actions and small pull requests to configuration files. To remove a post from the current snapshot, the owner adds its shortcode to `config/removals.txt`, merges the change, and runs the workflow or waits for the next scheduled run.

## Acceptance criteria

- A fresh repository can bootstrap an archive without committing Instagram credentials.
- A one-post manual sync downloads, processes, archives, builds, and deploys an eligible public post.
- A repeated sync produces no duplicate manifest or media entries.
- Private-account posts leave no post-level data in the snapshot or public build.
- Unsaving a public post does not remove it.
- A reconciled deleted post or newly private creator is removed from the current public snapshot.
- Images, videos, and mixed carousels render correctly in a single-column feed on mobile and desktop.
- Search, filter, sort, caption expansion, carousel controls, and direct source links work without a server.
- A failed sync leaves the prior archive branch and Pages deployment usable.
- The workflow refuses to publish an artifact at or above 900 MB or any file above 95 MiB.
- No test, generated page, artifact, or log exposes the Instagram session or GitHub secrets.

## Known limitations

- Instagram can break or block the unofficial Instaloader workflow at any time.
- GitHub-hosted runner IP addresses may trigger Instagram security challenges.
- A new or refreshed session requires a local interactive bootstrap.
- GitHub Pages cannot provide a private administration UI or server-side removal API.
- The 1 GB Pages limit makes the design unsuitable for a substantially larger or video-heavy archive.
- Removal from the current snapshot does not guarantee immediate purge from caches or unreachable Git storage.
- Instagram Collections cannot be reproduced with the selected toolchain.

