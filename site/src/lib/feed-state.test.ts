import { describe, expect, it } from "vitest";
import type { ArchivePost } from "../data/archive";
import { parseFeedState, selectPosts, serializeFeedState } from "./feed-state";

const post = (
  shortcode: string,
  overrides: Partial<ArchivePost> = {},
): ArchivePost => ({
  shortcode,
  creator_username: "ÉLODIE",
  creator_id: 1,
  source_url: `https://www.instagram.com/p/${shortcode}/`,
  caption: "Café 日本語 🌿",
  published_at: "2026-08-01T00:00:00Z",
  archived_at: "2026-09-01T00:00:00Z",
  verified_at: "2026-09-01T00:00:00Z",
  media_type: "image",
  publication_state: "published",
  unpublished_reason: null,
  media: [],
  ...overrides,
});

describe("feed state", () => {
  it("omits defaults and falls back from invalid values", () => {
    expect(parseFeedState("?type=images&sort=oldest")).toEqual({
      q: "",
      type: "all",
      sort: "archived",
    });
    expect(
      serializeFeedState(parseFeedState("?q=&type=all&sort=archived")),
    ).toBe("");
  });
  it("encodes search text and round-trips non-default state stably", () => {
    const state = {
      q: "café & 日本語+🌿",
      type: "carousel",
      sort: "published",
    } as const;
    expect(serializeFeedState(state)).toBe(
      "?q=caf%C3%A9+%26+%E6%97%A5%E6%9C%AC%E8%AA%9E%2B%F0%9F%8C%BF&type=carousel&sort=published",
    );
    expect(parseFeedState(serializeFeedState(state))).toEqual(state);
  });
  it.each(["éLoDiE", "CAFE\u0301", "日本語", "🌿"])(
    "finds Unicode username or caption %s without case sensitivity",
    (q) => {
      expect(
        selectPosts(
          [
            post("A"),
            post("B", { creator_username: "other", caption: "other" }),
          ],
          { q, type: "all", sort: "archived" },
        ).map((p) => p.shortcode),
      ).toEqual(["A"]);
    },
  );
  it.each(["image", "video", "carousel"] as const)(
    "matches exactly the %s media type",
    (type) => {
      const posts = [
        post("A"),
        post("B", { media_type: "video" }),
        post("C", { media_type: "carousel" }),
      ];
      expect(
        selectPosts(posts, { q: "", type, sort: "archived" }).map(
          (p) => p.media_type,
        ),
      ).toEqual([type]);
    },
  );
  it("combines search and media filtering", () => {
    expect(
      selectPosts(
        [post("A"), post("B", { media_type: "video", caption: "mountain" })],
        parseFeedState("?q=mountain&type=image"),
      ),
    ).toEqual([]);
  });
  it("orders by archive or publication date descending, including microseconds and shortcode ties, without mutating input", () => {
    const posts = [
      post("C"),
      post("B", { archived_at: "2026-09-01T00:00:00.000002Z" }),
      post("A", {
        archived_at: "2026-09-01T00:00:00.000002Z",
        published_at: "2026-08-02T00:00:00Z",
      }),
    ];
    expect(
      selectPosts(posts, parseFeedState("")).map((p) => p.shortcode),
    ).toEqual(["A", "B", "C"]);
    expect(
      selectPosts(posts, parseFeedState("?sort=published")).map(
        (p) => p.shortcode,
      ),
    ).toEqual(["A", "B", "C"]);
    expect(posts.map((p) => p.shortcode)).toEqual(["C", "B", "A"]);
  });
  it("changes ordering when publication dates differ from discovery dates", () => {
    const posts = [
      post("A", { published_at: "2026-08-01T00:00:00.000001Z" }),
      post("B", {
        archived_at: "2026-08-01T00:00:00Z",
        published_at: "2026-08-01T00:00:00.000002Z",
      }),
    ];
    expect(
      selectPosts(posts, parseFeedState("")).map((p) => p.shortcode),
    ).toEqual(["A", "B"]);
    expect(
      selectPosts(posts, parseFeedState("?sort=published")).map(
        (p) => p.shortcode,
      ),
    ).toEqual(["B", "A"]);
  });
});
