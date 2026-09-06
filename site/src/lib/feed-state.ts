import type { ArchivePost } from "../data/archive";

export interface FeedState {
  q: string;
  type: "all" | ArchivePost["media_type"];
  sort: "archived" | "published";
}

export function parseFeedState(search: string): FeedState {
  const params = new URLSearchParams(search);
  const type = params.get("type");
  return {
    q: params.get("q") ?? "",
    type:
      type === "image" || type === "video" || type === "carousel"
        ? type
        : "all",
    sort: params.get("sort") === "published" ? "published" : "archived",
  };
}

export function serializeFeedState(state: FeedState): string {
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  if (state.type !== "all") params.set("type", state.type);
  if (state.sort !== "archived") params.set("sort", state.sort);
  return params.size ? `?${params}` : "";
}

export function selectPosts(
  posts: ArchivePost[],
  state: FeedState,
): ArchivePost[] {
  const fold = (text: string) => text.normalize("NFC").toLowerCase();
  const query = fold(state.q.trim());
  // Preserve Python's microsecond timestamps; Date.parse truncates to milliseconds.
  const timestampKey = (value: string) => {
    const [seconds, fraction = ""] = value.slice(0, -1).split(".");
    return `${seconds}.${fraction.padEnd(6, "0")}`;
  };
  return posts
    .filter(
      (post) =>
        (state.type === "all" || post.media_type === state.type) &&
        (!query ||
          fold(post.creator_username).includes(query) ||
          fold(post.caption).includes(query)),
    )
    .sort((a, b) => {
      const key = state.sort === "published" ? "published_at" : "archived_at";
      const left = timestampKey(a[key]);
      const right = timestampKey(b[key]);
      return left < right
        ? 1
        : left > right
          ? -1
          : a.shortcode < b.shortcode
            ? -1
            : a.shortcode > b.shortcode
              ? 1
              : 0;
    });
}
