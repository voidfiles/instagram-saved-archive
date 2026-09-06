set shell := ["bash", "-o", "pipefail", "-c"]

default_repository := env_var_or_default("ARCHIVE_REPOSITORY", "github.com/voidfiles/instagram-saved-archive")
default_username := env_var_or_default("INSTAGRAM_USERNAME", "voidfiles")

# Store the Instagram username variable and a Chrome-derived session secret in GitHub.
bootstrap-session repository=default_repository username=default_username:
    session="$(uv run instagram-saved-archive bootstrap-session --username "{{ username }}")" && printf '%s\n' "$session" | gh secret set INSTAGRAM_SESSION_B64 --repo "{{ repository }}"
    gh variable set INSTAGRAM_USERNAME --repo "{{ repository }}" --body "{{ username }}"

# Run an end-to-end sync that attempts at most one newly saved post.
live-sync repository=default_repository:
    gh workflow run sync-and-deploy.yml --repo "{{ repository }}" --ref main -f max_new_posts=1 -f full_scan=false
    gh run list --repo "{{ repository }}" --workflow sync-and-deploy.yml --limit 5

# Install Python and site dependencies after checking local tool prerequisites.
setup:
    gh auth status || gh auth login
    node --version
    node -e 'if (process.versions.node.split(".")[0] !== "24") { console.error("Node.js 24.x is required."); process.exit(1); }'
    ffmpeg -version
    ffprobe -version
    (cd site && npm ci)
    uv sync --python 3.12 --group dev
    uv run --python 3.12 python --version
