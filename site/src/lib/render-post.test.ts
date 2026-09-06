// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ArchivePost, MediaRecord } from "../data/archive";
import { renderPost } from "./render-post";

const media: MediaRecord = {
  position: 0,
  kind: "image",
  asset: {
    asset_path: "media/A/a #?.webp",
    mime_type: "image/webp",
    width: 640,
    height: 480,
    byte_size: 1,
    sha256: "a".repeat(64),
  },
  preview: {
    asset_path: "media/A/preview.webp",
    mime_type: "image/webp",
    width: 320,
    height: 240,
    byte_size: 1,
    sha256: "b".repeat(64),
  },
};
const post: ArchivePost = {
  shortcode: "A",
  creator_username: "field.notes",
  creator_id: 1,
  source_url: "https://www.instagram.com/p/A/",
  caption: "Café 日本語 🌿",
  published_at: "2026-08-01T00:00:00Z",
  archived_at: "2026-09-01T00:00:00Z",
  verified_at: "2026-09-01T00:00:00Z",
  media_type: "image",
  publication_state: "published",
  unpublished_reason: null,
  media: [media],
};

describe("renderPost", () => {
  afterEach(() => vi.unstubAllEnvs());
  it("prefixes dynamically rendered images, srcsets, video posters and downloads", () => {
    vi.stubEnv("BASE_URL", "/owner-repo/");
    const row = renderPost(
      {
        ...post,
        media_type: "carousel",
        media: [
          media,
          {
            ...media,
            position: 1,
            kind: "video",
            duration_seconds: 2,
            asset: {
              ...media.asset,
              asset_path: "media/A/1.mp4",
              mime_type: "video/mp4",
            },
          },
        ],
      },
      document,
    );
    expect(row.querySelector("img")?.getAttribute("src")).toBe(
      "/owner-repo/archive/media/A/a%20%23%3F.webp",
    );
    expect(row.querySelector("img")?.getAttribute("srcset")).toBe(
      "/owner-repo/archive/media/A/preview.webp 320w, /owner-repo/archive/media/A/a%20%23%3F.webp 640w",
    );
    expect(row.querySelector("video")?.getAttribute("poster")).toBe(
      "/owner-repo/archive/media/A/preview.webp",
    );
    expect(row.querySelector("source")?.getAttribute("src")).toBe(
      "/owner-repo/archive/media/A/1.mp4",
    );
    expect(row.querySelector("video a")?.getAttribute("href")).toBe(
      "/owner-repo/archive/media/A/1.mp4",
    );
  });
  it("renders attribution, dates, and responsive local media with server-compatible hooks", () => {
    const row = renderPost(post, document);
    expect(row.dataset.shortcode).toBe("A");
    expect(row.getAttribute("aria-labelledby")).toBe("creator-A");
    expect(row.querySelector("h2 a")?.textContent).toBe("@field.notes");
    expect(row.querySelector(".source-link")?.getAttribute("rel")).toBe(
      "noopener noreferrer",
    );
    expect([...row.querySelectorAll("time")].map((t) => t.dateTime)).toEqual([
      post.published_at,
      post.archived_at,
    ]);
    const img = row.querySelector("img")!;
    expect(img.getAttribute("src")).toBe("/archive/media/A/a%20%23%3F.webp");
    expect(img.getAttribute("srcset")).toBe(
      "/archive/media/A/preview.webp 320w, /archive/media/A/a%20%23%3F.webp 640w",
    );
    expect(img.getAttribute("loading")).toBe("lazy");
    expect(img.width).toBe(640);
    expect(img.height).toBe(480);
  });
  it("keeps hostile captions and usernames as literal text", () => {
    const caption =
      '</script><img src=x onerror="alert(1)"><script>alert(1)</script>';
    const username = '<svg onload="alert(1)">';
    const row = renderPost(
      { ...post, caption, creator_username: username },
      document,
    );
    expect(row.querySelector("[data-caption-text]")?.textContent).toBe(caption);
    expect(row.querySelector("h2")?.textContent).toBe(`@${username}`);
    expect(row.querySelector("script, svg, [onerror], [onload]")).toBeNull();
    expect(row.querySelectorAll("img")).toHaveLength(1);
  });
  it.each([
    "../escape.webp",
    "/escape.webp",
    "https://evil.test/a.webp",
    "media\\a.webp",
    "media//a.webp",
    "media/./a.webp",
    "media/\0a.webp",
  ])("rejects unsafe local path %s", (asset_path) => {
    expect(() =>
      renderPost(
        {
          ...post,
          media: [{ ...media, asset: { ...media.asset, asset_path } }],
        },
        document,
      ),
    ).toThrow(/path/i);
  });
  it("rejects executable original links", () => {
    expect(() =>
      renderPost({ ...post, source_url: "javascript:alert(1)" }, document),
    ).toThrow(/url/i);
  });
  it("renders mixed carousels with native video controls and hidden progressive controls", () => {
    const video: MediaRecord = {
      ...media,
      position: 1,
      kind: "video",
      duration_seconds: 2,
      asset: {
        ...media.asset,
        asset_path: "media/A/1.mp4",
        mime_type: "video/mp4",
      },
    };
    const row = renderPost(
      {
        ...post,
        caption: "a".repeat(300),
        media_type: "carousel",
        media: [media, video],
      },
      document,
    );
    expect(row.querySelector("[data-carousel]")?.getAttribute("tabindex")).toBe(
      "0",
    );
    expect(row.querySelectorAll("[data-slide]")).toHaveLength(2);
    const player = row.querySelector("video")!;
    expect(player.controls).toBe(true);
    expect(player.autoplay).toBe(false);
    expect(player.getAttribute("poster")).toBe("/archive/media/A/preview.webp");
    expect(player.querySelector("source")?.getAttribute("src")).toBe(
      "/archive/media/A/1.mp4",
    );
    expect([...row.querySelectorAll("button")].every((b) => b.hidden)).toBe(
      true,
    );
    expect(
      row.querySelector("[data-caption-toggle]")?.getAttribute("aria-expanded"),
    ).toBe("true");
  });
  it("omits empty captions and single-media carousel controls", () => {
    const row = renderPost({ ...post, caption: "" }, document);
    expect(
      row.querySelector(".caption, [data-carousel], [data-carousel-next]"),
    ).toBeNull();
  });
});
