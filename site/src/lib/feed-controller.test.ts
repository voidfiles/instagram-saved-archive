// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ArchivePost } from "../data/archive";
import { mountFeed } from "./feed-controller";
import { renderPost } from "./render-post";

const asset = {
  asset_path: "media/A/0.webp",
  mime_type: "image/webp",
  width: 640,
  height: 480,
  byte_size: 1,
  sha256: "a".repeat(64),
};
const post = (
  shortcode: string,
  overrides: Partial<ArchivePost> = {},
): ArchivePost => ({
  shortcode,
  creator_username: "field.notes",
  creator_id: 1,
  source_url: `https://www.instagram.com/p/${shortcode}/`,
  caption: "mountain " + "long caption ".repeat(30),
  published_at: "2026-08-01T00:00:00Z",
  archived_at: "2026-09-01T00:00:00Z",
  verified_at: "2026-09-01T00:00:00Z",
  media_type: "image",
  publication_state: "published",
  unpublished_reason: null,
  media: [{ position: 0, kind: "image", asset, preview: asset }],
  ...overrides,
});
const posts = Array.from({ length: 26 }, (_, i) =>
  post(`POST${String(i).padStart(4, "0")}`),
);
const video = post("VIDEO0001", {
  caption: "sea",
  media_type: "video",
  media: [
    {
      position: 0,
      kind: "video",
      duration_seconds: 2,
      asset: { ...asset, asset_path: "media/V/0.mp4", mime_type: "video/mp4" },
      preview: asset,
    },
  ],
});
const carousel = post("CAROUSEL", {
  media_type: "carousel",
  media: [0, 1, 2].map((position) => ({
    position,
    kind: "image",
    asset,
    preview: asset,
  })),
});
let intersect: IntersectionObserverCallback;
let observer: IntersectionObserver;
let disconnected = false;
let cleanup: (() => void) | undefined;

beforeEach(() => {
  window.history.replaceState({}, "", "/");
  disconnected = false;
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      constructor(callback: IntersectionObserverCallback) {
        intersect = callback;
        observer = this as unknown as IntersectionObserver;
      }
      observe() {}
      unobserve() {}
      disconnect() {
        disconnected = true;
      }
    },
  );
  vi.stubGlobal("matchMedia", () => ({ matches: false }));
});
afterEach(() => {
  cleanup?.();
  cleanup = undefined;
  document.body.replaceChildren();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function setupFeed(search = "", data = [...posts, video]) {
  window.history.replaceState({}, "", `/${search}`);
  document.body.innerHTML = `<div data-feed-root><form data-feed-controls hidden><input type="search" data-feed-search aria-label="Search saved posts"><select data-feed-type><option value="all">All</option><option value="image">Images</option><option value="video">Videos</option><option value="carousel">Carousels</option></select><select data-feed-sort><option value="archived">Saved</option><option value="published">Posted</option></select></form><p data-feed-status role="status"></p><div data-post-list tabindex="-1"></div><div data-feed-sentinel></div><button data-feed-more hidden>Load more</button></div>`;
  const root = document.querySelector<HTMLElement>("[data-feed-root]")!;
  const list = root.querySelector<HTMLElement>("[data-post-list]")!;
  data.slice(0, 12).forEach((p) => list.append(renderPost(p, document)));
  const initialRow = list.firstElementChild;
  cleanup = mountFeed(root, data, window);
  return {
    root,
    list,
    initialRow,
    search: root.querySelector<HTMLInputElement>("[data-feed-search]")!,
    typeSelect: root.querySelector<HTMLSelectElement>("[data-feed-type]")!,
    sortSelect: root.querySelector<HTMLSelectElement>("[data-feed-sort]")!,
    visibleShortcodes: () =>
      [...list.children].map((row) => (row as HTMLElement).dataset.shortcode),
  };
}
function reveal(root: HTMLElement) {
  intersect(
    [
      {
        isIntersecting: true,
        target: root.querySelector("[data-feed-sentinel]")!,
      } as IntersectionObserverEntry,
    ],
    observer,
  );
}
function change(control: HTMLInputElement | HTMLSelectElement, value: string) {
  control.value = value;
  control.dispatchEvent(
    new Event(control instanceof HTMLInputElement ? "input" : "change", {
      bubbles: true,
    }),
  );
}
function pointer(target: HTMLElement, type: string, x: number, y = 10) {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.assign(event, {
    clientX: x,
    clientY: y,
    pointerId: 1,
    button: 0,
    isPrimary: true,
  });
  target.dispatchEvent(event);
}
function setupCarousel(reduced = false) {
  vi.stubGlobal("matchMedia", () => ({ matches: reduced }));
  const mounted = setupFeed("", [carousel]);
  const gallery = mounted.root.querySelector<HTMLElement>("[data-carousel]")!;
  const track = gallery.querySelector<HTMLElement>(".media-track")!;
  Object.defineProperty(track, "clientWidth", { value: 640 });
  const scroll = vi.fn((options: ScrollToOptions) => {
    track.scrollLeft = options.left!;
    track.dispatchEvent(new Event("scroll"));
  });
  Object.defineProperty(track, "scrollTo", { value: scroll });
  return {
    ...mounted,
    gallery,
    track,
    scroll,
    next: gallery.querySelector<HTMLButtonElement>("[data-carousel-next]")!,
    prev: gallery.querySelector<HTMLButtonElement>("[data-carousel-prev]")!,
    status: gallery.querySelector<HTMLElement>("[data-carousel-status]")!,
  };
}

describe("feed controller", () => {
  it("leaves existing media attached when revealing another batch", () => {
    const mounted = setupFeed();
    const mutations = new MutationObserver(() => {});
    mutations.observe(mounted.list, { childList: true });
    reveal(mounted.root);
    expect(
      mutations
        .takeRecords()
        .reduce((count, record) => count + record.removedNodes.length, 0),
    ).toBe(0);
    mutations.disconnect();
  });
  it("keeps the first 12 server rows, reveals batches, and makes no data requests", () => {
    const fetch = vi.fn(() => {
      throw new Error("unexpected network");
    });
    vi.stubGlobal("fetch", fetch);
    const xhr = vi.spyOn(XMLHttpRequest.prototype, "open");
    const mounted = setupFeed();
    expect(mounted.list.children).toHaveLength(12);
    expect(mounted.list.firstElementChild).toBe(mounted.initialRow);
    expect(
      mounted.root.querySelector<HTMLElement>("[data-feed-controls]")!.hidden,
    ).toBe(false);
    reveal(mounted.root);
    expect(mounted.list.children).toHaveLength(24);
    reveal(mounted.root);
    expect(mounted.list.children).toHaveLength(27);
    reveal(mounted.root);
    expect(mounted.list.children).toHaveLength(27);
    expect(fetch).not.toHaveBeenCalled();
    expect(xhr).not.toHaveBeenCalled();
  });
  it("offers a keyboard-accessible batch reveal without IntersectionObserver", () => {
    vi.stubGlobal("IntersectionObserver", undefined);
    const mounted = setupFeed();
    const more =
      mounted.root.querySelector<HTMLButtonElement>("[data-feed-more]")!;
    expect(more.hidden).toBe(false);
    more.focus();
    more.click();
    expect(mounted.list.children).toHaveLength(24);
    expect(document.activeElement).toBe(more);
    more.click();
    expect(mounted.list.children).toHaveLength(27);
    expect(document.activeElement).not.toBe(document.body);
  });
  it("updates query state via pushState while preserving search focus and selection", () => {
    const mounted = setupFeed();
    const push = vi.spyOn(window.history, "pushState");
    mounted.search.focus();
    change(mounted.search, "mountain");
    expect(window.location.search).toBe("?q=mountain");
    expect(push).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(mounted.search);
    change(mounted.typeSelect, "image");
    change(mounted.sortSelect, "published");
    expect(window.location.search).toBe(
      "?q=mountain&type=image&sort=published",
    );
    change(mounted.search, "no matches");
    expect(mounted.list.children).toHaveLength(0);
    expect(
      mounted.root.querySelector("[data-feed-status]")?.textContent,
    ).toMatch(/no.*posts/i);
  });
  it("restores filters and bounded results on browser navigation", () => {
    const mounted = setupFeed("?q=mountain&type=image&sort=published");
    expect(mounted.search.value).toBe("mountain");
    expect(mounted.sortSelect.value).toBe("published");
    window.history.pushState({}, "", "?type=video");
    window.dispatchEvent(new PopStateEvent("popstate"));
    expect(mounted.typeSelect.value).toBe("video");
    expect(mounted.search.value).toBe("");
    expect(mounted.visibleShortcodes()).toEqual(["VIDEO0001"]);
  });
  it("preserves focus in retained rows and moves focus to results when the focused row disappears", () => {
    const mounted = setupFeed();
    const link = mounted.list.querySelector<HTMLAnchorElement>("a")!;
    link.focus();
    window.history.pushState({}, "", "?type=image");
    window.dispatchEvent(new PopStateEvent("popstate"));
    expect(document.activeElement).toBe(link);
    window.history.pushState({}, "", "?type=video");
    window.dispatchEvent(new PopStateEvent("popstate"));
    expect(document.activeElement).toBe(mounted.list);
  });
  it("collapses long captions only on mount and toggles accessible disclosure for new rows too", () => {
    const mounted = setupFeed();
    reveal(mounted.root);
    const button =
      mounted.list.lastElementChild!.querySelector<HTMLButtonElement>(
        "[data-caption-toggle]",
      )!;
    const caption = document.getElementById(
      button.getAttribute("aria-controls")!,
    )!;
    expect(button.hidden).toBe(false);
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(caption.classList.contains("is-collapsed")).toBe(true);
    button.focus();
    button.click();
    expect(button.getAttribute("aria-expanded")).toBe("true");
    expect(caption.classList.contains("is-collapsed")).toBe(false);
    expect(document.activeElement).toBe(button);
    button.click();
    expect(button.getAttribute("aria-expanded")).toBe("false");
  });
  it("cleans up all interaction, observer, and browser-history listeners", () => {
    const mounted = setupFeed();
    cleanup!();
    cleanup = undefined;
    change(mounted.search, "absent");
    window.history.pushState({}, "", "?type=video");
    window.dispatchEvent(new PopStateEvent("popstate"));
    reveal(mounted.root);
    expect(mounted.list.children).toHaveLength(12);
    expect(disconnected).toBe(true);
  });
});

describe("carousel interactions", () => {
  it.each([false, true])(
    "scrolls the track, clamps endpoints and reports position with reduced motion=%s",
    (reduced) => {
      const m = setupCarousel(reduced);
      expect(m.next.hidden).toBe(false);
      expect(m.status.textContent).toBe("1 of 3");
      m.prev.click();
      expect(m.status.textContent).toBe("1 of 3");
      m.next.focus();
      m.next.click();
      expect(m.scroll).toHaveBeenLastCalledWith({
        left: 640,
        behavior: reduced ? "auto" : "smooth",
      });
      expect(m.status.textContent).toBe("2 of 3");
      expect(document.activeElement).toBe(m.next);
      m.next.click();
      m.next.click();
      expect(m.status.textContent).toBe("3 of 3");
      m.track.scrollLeft = 0;
      m.track.dispatchEvent(new Event("scroll"));
      expect(m.status.textContent).toBe("1 of 3");
    },
  );
  it("handles arrows only with focus within a carousel, including the section and its buttons", () => {
    const m = setupCarousel();
    m.search.focus();
    m.search.dispatchEvent(
      new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }),
    );
    expect(m.scroll).not.toHaveBeenCalled();
    m.gallery.focus();
    m.gallery.dispatchEvent(
      new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }),
    );
    expect(m.status.textContent).toBe("2 of 3");
    m.next.focus();
    m.next.dispatchEvent(
      new KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }),
    );
    expect(m.status.textContent).toBe("1 of 3");
  });
  it("handles arrow navigation from a focused media element inside the carousel", () => {
    const m = setupCarousel();
    const player = document.createElement("video");
    player.tabIndex = 0;
    m.track.append(player);
    player.focus();
    player.dispatchEvent(
      new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }),
    );
    expect(m.status.textContent).toBe("2 of 3");
  });
  it("requires horizontal swipe threshold, captures pointers, and ignores vertical/cancelled gestures", () => {
    const m = setupCarousel();
    const capture = vi.fn();
    const release = vi.fn();
    m.gallery.setPointerCapture = capture;
    m.gallery.releasePointerCapture = release;
    pointer(m.track, "pointerdown", 200);
    pointer(m.track, "pointerup", 180);
    expect(m.status.textContent).toBe("1 of 3");
    pointer(m.track, "pointerdown", 200);
    pointer(m.track, "pointerup", 120, 160);
    expect(m.status.textContent).toBe("1 of 3");
    pointer(m.track, "pointerdown", 200);
    pointer(m.track, "pointerup", 120);
    expect(m.status.textContent).toBe("2 of 3");
    expect(capture).toHaveBeenCalledWith(1);
    expect(release).toHaveBeenCalledWith(1);
    pointer(m.track, "pointerdown", 100);
    pointer(m.track, "pointercancel", 200);
    pointer(m.track, "pointerup", 200);
    expect(m.status.textContent).toBe("2 of 3");
  });
});
