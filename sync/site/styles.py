"""Styles for the generated static archive."""

DEFAULT_CSS = """\
:root {
  color-scheme: light dark;
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  line-height: 1.5;
  background: Canvas;
  color: CanvasText;
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
}

a {
  color: LinkText;
  text-underline-offset: 0.18em;
}

a:focus-visible,
video:focus-visible {
  outline: 0.2rem solid Highlight;
  outline-offset: 0.2rem;
}

.skip-link {
  position: absolute;
  inset-block-start: 0.5rem;
  inset-inline-start: 0.5rem;
  padding: 0.5rem 0.75rem;
  background: Canvas;
  transform: translateY(-200%);
  z-index: 1;
}

.skip-link:focus {
  transform: translateY(0);
}

.page-header,
#posts {
  width: min(100% - 2rem, 48rem);
  margin-inline: auto;
}

.page-header {
  padding-block: 3rem 1.5rem;
}

.page-header h1 {
  margin: 0;
  font-size: clamp(2rem, 8vw, 4rem);
  line-height: 1;
}

.archive-status {
  margin-block-end: 0;
  color: GrayText;
}

#posts {
  display: grid;
  gap: 2rem;
  padding-block-end: 4rem;
}

.post {
  min-width: 0;
  overflow: hidden;
  border: 1px solid color-mix(in srgb, CanvasText 20%, transparent);
  border-radius: 0.75rem;
  background: Canvas;
}

.post-header,
.caption {
  padding-inline: clamp(1rem, 4vw, 1.5rem);
}

.post-header {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.5rem 1rem;
  padding-block: 1rem;
}

.post-header h2 {
  margin: 0;
  font-size: 1rem;
}

.post-header time {
  color: GrayText;
  font-size: 0.875rem;
}

.media {
  display: grid;
  gap: 0.75rem;
  margin: 0;
  padding: 0;
  list-style: none;
}

.media figure {
  margin: 0;
}

.media img,
.media video {
  display: block;
  width: 100%;
  height: auto;
  max-height: 80vh;
  object-fit: contain;
  background: #111;
}

.caption {
  margin: 0;
  padding-block: 1rem 1.25rem;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

@media (max-width: 32rem) {
  .page-header,
  #posts {
    width: min(100% - 1rem, 48rem);
  }
}
"""

__all__ = ["DEFAULT_CSS"]
