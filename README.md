# Instagram Saved Archive

A public, single-column archive of saved Instagram posts. Python downloads and normalizes eligible media into a validated snapshot; Astro builds a static feed with search, media filters, sorting, expandable captions, carousels, and links to the creators and original posts. GitHub Actions runs the sync and deploys GitHub Pages.

## Before you publish

The deployed site, captions, creator attribution, exported manifest, and archived media are public. A source being public does not grant permission to republish it. Publish only material you have the rights or permission to share, and respect creators' removal requests and Instagram and GitHub terms and limits. This project does not bypass access controls, checkpoints, or account restrictions. It depends on an unofficial Instagram client; Instagram changes and throttling can interrupt it.

Private posts are skipped without durable post metadata or media, even if your account can view them. Previously archived posts that are later confirmed private or deleted are removed from the current snapshot during rotating reconciliation. This is not immediate monitoring. Unsaving a post does **not** remove it from the archive; use the removal list below.

The [MIT license](LICENSE) covers this repository's code and documentation only. It does not license archived creator media or captions. Those rights remain with their respective owners.

## Local setup

Use Python **3.12**, Node.js **24**, and **FFmpeg 6+** with `ffmpeg` and `ffprobe` on `PATH` (including H.264/libx264 support). The commands below use Bash on Linux/macOS. Install Git and the GitHub CLI (`gh`) too. Use a repository you own with a `main` branch and GitHub Pages/Actions available; fork or copy this project into it first.

On Debian/Ubuntu, install the matching `python3.12-venv` package if `python3.12 -m venv` reports that `ensurepip` is unavailable. The virtual environment must include pip before continuing.

Replace the two example values, authenticate `gh`, and clone your repository:

```bash
ARCHIVE_REPOSITORY=YOUR_GITHUB_OWNER/YOUR_REPOSITORY
INSTAGRAM_USERNAME=YOUR_INSTAGRAM_USERNAME
gh auth login
gh repo clone "$ARCHIVE_REPOSITORY" instagram-saved-archive
cd instagram-saved-archive
python3.12 -m venv .venv
source .venv/bin/activate
python --version
node --version
ffmpeg -version
ffprobe -version
python -m pip install --upgrade pip
python -m pip install --group dev -e .
(cd site && npm ci)
```

The pip upgrade supplies support for `--group dev` (pip 25.1+). Python dependencies are pinned in `pyproject.toml`; site dependencies are pinned in `site/package-lock.json`. Keep those pins and use `npm ci`. Production installation uses pip, not uv. In a new shell, return to the repository, activate `.venv`, and set the two example variables again. The static checker invokes `python` from the active environment; an optional `PYTHON=/absolute/path/to/python` selects another interpreter with the package installed.

## Configure GitHub and bootstrap your session

In your repository's **Settings → Pages → Build and deployment**, set **Source** to **GitHub Actions**. Enable Actions if prompted for a fork. Allow the sync workflow's requested contents, Pages, and identity-token permissions; repository or organization policies must permit these permissions and updates to the `archive-data` branch. The workflow deploys through the `github-pages` environment, so check any environment approval rules too.

Set the repository Actions variable `INSTAGRAM_USERNAME` and the repository Actions secret `INSTAGRAM_SESSION_B64`. Create the session **locally in an interactive terminal**, with your password and any two-factor code entered only at the prompts:

```bash
gh variable set INSTAGRAM_USERNAME --repo "$ARCHIVE_REPOSITORY" --body "$INSTAGRAM_USERNAME"
set -o pipefail
instagram-saved-archive bootstrap-session --username "$INSTAGRAM_USERNAME" \
  | gh secret set INSTAGRAM_SESSION_B64 --repo "$ARCHIVE_REPOSITORY"
```

The bootstrap command keeps password and two-factor prompts on stderr while suppressing raw upstream diagnostics and sanitizing rejected-credential retry messages. Successful stdout contains only the base64 session and one newline; the pipe transfers it directly to GitHub Secrets without a file or clipboard step. Do not combine stderr with stdout (`2>&1`), enable shell tracing (`set -x`), paste the encoded session into source, or upload it as an artifact. Base64 is encoding, not encryption: treat this session as a login credential. Only load sessions you created and trust; the underlying session format is not safe for untrusted input.

Bootstrap verifies the authenticated account matches the requested username. Its temporary session file is mode `0600` and is deleted when bootstrap completes or raises an ordinary exception. A forced process kill can prevent cleanup; if that happens, locate only that run's `instagram-session-*` temporary file in the system temporary directory and remove it. The Actions sync also creates a temporary mode `0600` session and removes it on shell exit. Do not retain decoded credentials in the checkout.

If either side of the pipe fails, stop and repeat the session setup successfully before running a sync. Complete any Instagram checkpoint in the official app/site, then retry locally. To replace an expired or revoked session, rerun the same bootstrap-to-`gh secret set` pipe; it replaces `INSTAGRAM_SESSION_B64`. If you change accounts, update `INSTAGRAM_USERNAME` as well. Revoke the account session in Instagram when you no longer want it used, and remove the GitHub secret when retiring the archive.

## First run: one post

Start with an empty `config/removals.txt` (the checked-in file is intentionally zero bytes). In **Actions → Sync archive and deploy Pages → Run workflow**, choose `main`, set `max_new_posts` to `1`, and leave `full_scan` false. The equivalent command is:

```bash
gh workflow run sync-and-deploy.yml --repo "$ARCHIVE_REPOSITORY" --ref main \
  -f max_new_posts=1 -f full_scan=false
gh run list --repo "$ARCHIVE_REPOSITORY" --workflow sync-and-deploy.yml --limit 5
```

Open that run and its job summary. The first run restores `archive-data` if it exists, or initializes an empty snapshot when the branch is absent. It authenticates, attempts at most one new eligible public post, validates and exports the result, builds and checks the real site, publishes the snapshot, and finally uploads/deploys Pages. Private or unavailable posts do not consume the new-post allowance. An invalid media item can be skipped, so one attempted post does not guarantee one published post; inspect the aggregate counts and any public media-failure shortcodes.

Confirm the run succeeded, `archive-data` exists, and the deployment URL shows the expected image/video/carousel, caption, attribution, and working original link. Verify every media URL loads. The Pages configuration supplies the build base automatically, supporting a root site, a custom domain with an empty base, and a project site such as `/YOUR_REPOSITORY/`. Source code remains on `main`; do not configure Pages to build the `archive-data` branch.

## Routine operation and backfill

The scheduled workflow requests up to 50 new posts daily at **10:17 UTC**. GitHub may delay scheduled runs. All scheduled/manual runs share one concurrency group and do not cancel a run already in progress. The `Test` workflow runs offline checks on pull requests and pushes to `main`; it does not access Instagram or deploy.

For backfill, repeat the manual sync with `max_new_posts=50` (or any integer from 1 through 50). Each run starts at the current saved feed, skips known shortcodes without redownloading them, and attempts the next batch. `Backfill complete: yes` is recorded only after reaching the end of the saved feed; hitting a batch limit does not prove completion. Once complete, ordinary discovery may stop after 20 consecutive known public posts.

Use a full scan when you want to bypass that known-post stopping rule, for example to check for older saved posts:

```bash
gh workflow run sync-and-deploy.yml --repo "$ARCHIVE_REPOSITORY" --ref main \
  -f max_new_posts=50 -f full_scan=true
```

Full scan still respects the new-post batch limit and does not reverify the whole existing archive at once. Every successful run separately reconciles up to 20 archived posts, rotating through them across runs. Only definitive private/deleted results trigger automatic removals; transport and authentication errors abort the transaction. Large archives therefore take multiple runs to reverify completely.

## Remove an archived post

Edit `config/removals.txt` on `main`, commit and push it, then run a manual sync. Put one Instagram **shortcode** per line, taken from the original post's `/p/SHORTCODE/` (or `/reel/SHORTCODE/`) URL. Blank lines and lines beginning with `#` are ignored; duplicates are harmless. Use only letters, digits, `_`, and `-`; do not paste full URLs or inline comments.

```text
# Shortcodes to exclude from future snapshots
EXAMPLE_SHORTCODE
AnotherExample_123
```

Keep the shortcodes listed to prevent reimporting a still-saved post. On a successful run, listed posts and their media are removed from the current snapshot and rebuilt site. Removing a line makes that post eligible for discovery again. Review the exact file change before committing:

```bash
git diff -- config/removals.txt
git add config/removals.txt
git commit -m "Update archive removals"
git push origin main
gh workflow run sync-and-deploy.yml --repo "$ARCHIVE_REPOSITORY" --ref main \
  -f max_new_posts=1 -f full_scan=false
```

`archive-data` is replaced with a single parentless snapshot commit after all sync/build/static gates succeed; it is not an accumulating history branch or a backup service. Removal affects the current branch and new Pages deployment, **not guaranteed erasure**. Prior Git objects, GitHub artifacts, Pages/CDN/browser caches, clones, forks, downloads, and third-party copies can persist. The pipeline cannot retract those copies. Retain only authorized offline backups, and use the relevant platform's removal process if publication needs to be withdrawn urgently.

## Size limits and failure recovery

Both snapshot and site artifact budgets warn at **850,000,000 bytes (850 MB)** and reject at **900,000,000 bytes (900 MB)**. Any generated file larger than **95 MiB (99,614,720 bytes)** is rejected; exactly 95 MiB is allowed. WebP images/previews and MP4 videos are generated locally; no remote Instagram CDN media is used by the deployed feed. These project limits are guardrails, not a guarantee of GitHub storage, bandwidth, or account availability.

Inspect a local snapshot or build with:

```bash
instagram-saved-archive check-budget --root site/dist
```

It emits JSON including total bytes and the ten largest files; diagnostics go to stderr. Use authorized removals to reduce the archive before retrying. Do not bypass a size check or hand-edit manifest hashes to force publication.

The job summary provides stable, sanitized diagnostics. CLI exit codes are:

| Code | Meaning | Recovery |
| --- | --- | --- |
| 0 | Success | Check the Pages deployment and aggregate counts. |
| 2 | Invalid arguments | Correct username/required inputs or the 1–50 batch bound. |
| 20 | Authentication, checkpoint, or interrupted login | Complete account checks locally, bootstrap again, and replace the session secret. |
| 21 | Instagram throttling | Wait before retrying; avoid repeated manual requests. |
| 22 | Transport retries exhausted | Retry later after connectivity/service recovery. |
| 30 | Invalid snapshot, paths, or media validation | Inspect the failing gate and validate a trusted snapshot; do not publish damaged data. |
| 31 | Size refusal | Review largest files, add authorized removals, and retry. |
| 40 | Archive publication failure | Check repository identity, branch rules, workflow permissions, and remote availability. |
| 1 | Unexpected failure | Inspect the failed step, preserve the prior snapshot, and diagnose before retrying. |

Sync stages changes transactionally. A failed sync or a failed production build/static check prevents archive publication and Pages deployment, preserving the previously published snapshot/site. A later Pages upload/deploy failure can occur **after** the new `archive-data` snapshot was published; the old Pages site may remain live while the branch is newer. Fix the Pages permissions/configuration or service failure and rerun the workflow; known items are not redownloaded. Never force-push `main` or delete `archive-data` as a routine recovery step.

If a local sync is interrupted, rerun `instagram-saved-archive sync` with the same snapshot path, session, username, and removals arguments. The CLI recovers the owned transaction before validating the snapshot or contacting Instagram, including when a hard interruption left the snapshot directory temporarily absent between renames. It refuses symlinked paths or transaction directories that do not match the recorded ownership. Do not initialize a replacement directory, manually remove recovery markers/backups, or merge partially staged data. For corrupt durable data, keep an offline copy and recover from a previously validated snapshot; a force-replaced branch may not provide a recoverable previous version. The workflow's remote restore step distinguishes an absent branch from network/authentication failure and fails on the latter instead of initializing over it.

## Offline development and verification

With `.venv` active and the supported tools installed, run this complete fixture suite from the repository root. It creates generated sample media and never needs Instagram credentials:

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
npx playwright install chromium
npm run test:e2e
npm run preview -- --host 127.0.0.1 --port 4321
```

On Linux, if Chromium reports missing system libraries, use `npx playwright install --with-deps chromium` with the system package-manager privileges it requests. The browser suite checks 375×812, 768×1024, and 1440×1000 viewports. Open `http://127.0.0.1:4321/` to inspect the fixture manually; stop preview with Ctrl-C. Fixture media, site inputs, build outputs, browser traces/screenshots, and `.tmp/` are ignored. Remove generated local files only when you no longer need them; do not commit local sessions or real archive exports.

To prove a project Pages build locally, from `site/`:

```bash
ASTRO_BASE_PATH=/owner-repo npm run build
ASTRO_BASE_PATH=/owner-repo npm run check:static
ASTRO_BASE_PATH=/owner-repo npm run preview -- --host 127.0.0.1 --port 4321
```

Open `http://127.0.0.1:4321/owner-repo/`. Asset files stay at `dist/_astro/` and `dist/archive/`; their public URLs include `/owner-repo/`. Use the same base for build, static checking, and preview. Stop preview, then run `npm run build` again without that environment variable before the root-path browser suite. An unset or empty `ASTRO_BASE_PATH` builds at `/`.
