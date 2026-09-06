import type { ArchivePost } from "../data/archive";

// This boundary must remain browser-only: the build-time loader uses node:fs.
function localAssetUrl(path: string): string {
  if (
    /[\\:\0]/.test(path) ||
    path.split("/").some((part) => !part || part === "." || part === "..")
  ) {
    throw new Error("Invalid local asset path");
  }
  return `/archive/${path.split("/").map(encodeURIComponent).join("/")}`;
}

/** Keep markup and accessibility hooks aligned with PostCard, MediaGallery and Caption. */
export function renderPost(post: ArchivePost, document: Document): HTMLElement {
  const source = new URL(post.source_url);
  if (source.protocol !== "https:") throw new Error("Invalid source URL");
  const element = <K extends keyof HTMLElementTagNameMap>(
    tag: K,
    attrs: Record<string, string | number | boolean | undefined> = {},
    text?: string,
  ): HTMLElementTagNameMap[K] => {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attrs)) {
      if (value !== undefined && value !== false)
        node.setAttribute(name, value === true ? "" : String(value));
    }
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const external = { target: "_blank", rel: "noopener noreferrer" };
  const row = element("article", {
    class: "post-row",
    "data-shortcode": post.shortcode,
    "data-media-type": post.media_type,
    "aria-labelledby": `creator-${post.shortcode}`,
  });
  const header = element("header", { class: "post-header" });
  const creator = element("div");
  const heading = element("h2", { id: `creator-${post.shortcode}` });
  heading.append(
    element(
      "a",
      {
        href: `https://www.instagram.com/${encodeURIComponent(post.creator_username)}/`,
        ...external,
      },
      `@${post.creator_username}`,
    ),
  );
  creator.append(
    element(
      "p",
      { class: "eyebrow" },
      post.media_type === "carousel" ? "Collection" : post.media_type,
    ),
    heading,
  );
  const original = element(
    "a",
    {
      class: "source-link",
      href: post.source_url,
      ...external,
      "aria-label": `View original post by @${post.creator_username} (opens in a new tab)`,
    },
    "View original ",
  );
  original.append(element("span", { "aria-hidden": "true" }, "↗"));
  header.append(creator, original);

  const carousel = post.media.length > 1;
  const gallery = element("section", {
    class: "media-gallery",
    "data-carousel": carousel ? "" : undefined,
    "aria-label": `Media by @${post.creator_username}`,
    "aria-roledescription": carousel ? "carousel" : undefined,
    tabindex: carousel ? "0" : undefined,
  });
  const track = element("div", {
    class: "media-track",
    id: `media-${post.shortcode}`,
  });
  post.media.forEach((item, index) => {
    const figure = element("figure", {
      class: "media-slide",
      "data-slide": index,
      role: carousel ? "group" : undefined,
      "aria-roledescription": carousel ? "slide" : undefined,
      "aria-label": carousel
        ? `${index + 1} of ${post.media.length}`
        : undefined,
    });
    const asset = localAssetUrl(item.asset.asset_path);
    const preview = localAssetUrl(item.preview.asset_path);
    if (item.kind === "image") {
      figure.append(
        element("img", {
          src: asset,
          srcset:
            item.preview.width !== item.asset.width
              ? `${preview} ${item.preview.width}w, ${asset} ${item.asset.width}w`
              : undefined,
          sizes: "(max-width: 792px) calc(100vw - 32px), 760px",
          width: item.asset.width,
          height: item.asset.height,
          alt: `Saved image ${index + 1} by @${post.creator_username}`,
          loading: "lazy",
          decoding: "async",
          draggable: "false",
        }),
      );
    } else {
      const video = element("video", {
        controls: true,
        preload: "metadata",
        poster: preview,
        width: item.asset.width,
        height: item.asset.height,
        playsinline: true,
        "aria-label": `Saved video ${index + 1} by @${post.creator_username}`,
      });
      const fallback = element(
        "p",
        {},
        "Your browser cannot play this video. ",
      );
      fallback.append(element("a", { href: asset }, "Download the video"), ".");
      video.append(
        element("source", { src: asset, type: item.asset.mime_type }),
        fallback,
      );
      figure.append(video);
    }
    track.append(figure);
  });
  gallery.append(track);
  if (carousel) {
    const controls = element("div", { class: "carousel-controls" });
    controls.append(
      element(
        "button",
        {
          type: "button",
          "data-carousel-prev": "",
          "aria-label": "Previous slide",
          "aria-controls": track.id,
          hidden: true,
        },
        "←",
      ),
      element(
        "p",
        {
          class: "carousel-status",
          "data-carousel-status": "",
          "aria-live": "polite",
        },
        `${post.media.length} slides · Scroll to explore`,
      ),
      element(
        "button",
        {
          type: "button",
          "data-carousel-next": "",
          "aria-label": "Next slide",
          "aria-controls": track.id,
          hidden: true,
        },
        "→",
      ),
    );
    gallery.append(controls);
  }
  const details = element("div", { class: "post-details" });
  if (post.caption) {
    const caption = element("div", { class: "caption" });
    caption.append(
      element(
        "p",
        { id: `caption-${post.shortcode}`, "data-caption-text": "" },
        post.caption,
      ),
    );
    if (post.caption.length > 280)
      caption.append(
        element(
          "button",
          {
            type: "button",
            class: "text-button",
            "data-caption-toggle": "",
            "aria-expanded": "true",
            "aria-controls": `caption-${post.shortcode}`,
            hidden: true,
          },
          "Show less",
        ),
      );
    details.append(caption);
  }
  const dates = element("footer", { class: "post-dates" });
  for (const [label, value] of [
    ["Posted", post.published_at],
    ["Saved", post.archived_at],
  ]) {
    const span = element("span", {}, `${label} `);
    span.append(
      element(
        "time",
        { datetime: value },
        new Intl.DateTimeFormat("en", {
          dateStyle: "medium",
          timeZone: "UTC",
        }).format(new Date(value)),
      ),
    );
    dates.append(span);
  }
  details.append(dates);
  row.append(header, gallery, details);
  return row;
}
