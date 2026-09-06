import type { ArchivePost } from "../data/archive";
import { parseFeedState, selectPosts, serializeFeedState } from "./feed-state";
import { renderPost } from "./render-post";

/** Enhance server content locally; the returned function releases every owned listener. */
export function mountFeed(
  root: HTMLElement,
  posts: ArchivePost[],
  window: Window,
): () => void {
  const document = root.ownerDocument;
  const list = root.querySelector<HTMLElement>("[data-post-list]")!;
  const status = root.querySelector<HTMLElement>("[data-feed-status]")!;
  const controls = root.querySelector<HTMLFormElement>("[data-feed-controls]")!;
  const search = root.querySelector<HTMLInputElement>("[data-feed-search]")!;
  const type = root.querySelector<HTMLSelectElement>("[data-feed-type]")!;
  const sort = root.querySelector<HTMLSelectElement>("[data-feed-sort]")!;
  const sentinel = root.querySelector<HTMLElement>("[data-feed-sentinel]")!;
  const more = root.querySelector<HTMLButtonElement>("[data-feed-more]")!;
  const rows = new Map<string, HTMLElement>();
  list
    .querySelectorAll<HTMLElement>("[data-shortcode]")
    .forEach((row) => rows.set(row.dataset.shortcode!, row));
  const enhanced = new WeakSet<HTMLElement>();
  const positions = new WeakMap<HTMLElement, number>();
  const navigating = new WeakSet<HTMLElement>();
  let state = parseFeedState(window.location.search);
  let selected = selectPosts(posts, state);
  let limit = 12;
  let active = true;
  let gesture:
    | {
        gallery: HTMLElement;
        id: number;
        x: number;
        y: number;
        videoOrigin: boolean;
        captured: boolean;
      }
    | undefined;
  let suppressClickGallery: HTMLElement | undefined;
  const disposers: (() => void)[] = [];
  function listen(
    target: EventTarget,
    name: string,
    handler: (event: Event) => void,
    capture = false,
  ) {
    target.addEventListener(name, handler, capture);
    disposers.push(() => target.removeEventListener(name, handler, capture));
  }
  function setPosition(gallery: HTMLElement, index: number) {
    const count = gallery.querySelectorAll("[data-slide]").length;
    const position = Math.max(0, Math.min(count - 1, index));
    positions.set(gallery, position);
    gallery.querySelector<HTMLElement>("[data-carousel-status]")!.textContent =
      `${position + 1} of ${count}`;
    gallery
      .querySelector("[data-carousel-prev]")!
      .setAttribute("aria-disabled", String(position === 0));
    gallery
      .querySelector("[data-carousel-next]")!
      .setAttribute("aria-disabled", String(position === count - 1));
    return position;
  }
  function navigate(gallery: HTMLElement, delta: number) {
    const track = gallery.querySelector<HTMLElement>(".media-track")!;
    const position = setPosition(
      gallery,
      (positions.get(gallery) ?? 0) + delta,
    );
    // Preserve the requested destination while smooth scrolling crosses earlier slides.
    if (Math.abs(track.scrollLeft - track.clientWidth * position) > 1)
      navigating.add(gallery);
    track.scrollTo({
      left: track.clientWidth * position,
      behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
        ? "auto"
        : "smooth",
    });
  }
  function enhance(row: HTMLElement) {
    if (enhanced.has(row)) return;
    enhanced.add(row);
    const button = row.querySelector<HTMLButtonElement>(
      "[data-caption-toggle]",
    );
    if (button) {
      button.hidden = false;
      button.setAttribute("aria-expanded", "false");
      button.textContent = "Show more";
      row.querySelector("[data-caption-text]")!.classList.add("is-collapsed");
    }
    const gallery = row.querySelector<HTMLElement>("[data-carousel]");
    if (gallery) {
      gallery.dataset.enhanced = "";
      gallery
        .querySelectorAll<HTMLButtonElement>("button")
        .forEach((button) => {
          button.hidden = false;
        });
      setPosition(gallery, 0);
    }
  }
  function render() {
    const focused = document.activeElement as HTMLElement | null;
    const focusWasInList = !!focused && list.contains(focused);
    const visible = selected.slice(0, limit).map((post) => {
      let row = rows.get(post.shortcode);
      if (!row) {
        row = renderPost(post, document);
        rows.set(post.shortcode, row);
      }
      enhance(row);
      return row;
    });
    // Keep existing media attached during progressive reveal so playback is not reset.
    const existing = [...list.children];
    if (existing.every((row, index) => visible[index] === row)) {
      list.append(...visible.slice(existing.length));
    } else {
      list.replaceChildren(...visible);
    }
    if (focusWasInList)
      (focused && list.contains(focused) ? focused : list).focus({
        preventScroll: true,
      });
    const hasMore = visible.length < selected.length;
    if (!hasMore && focused === more) list.focus({ preventScroll: true });
    more.hidden = !hasMore;
    sentinel.hidden = !hasMore;
    status.textContent = selected.length
      ? `Showing ${visible.length} of ${selected.length} saved posts`
      : posts.length
        ? "No saved posts match your filters."
        : "No saved posts yet.";
  }
  function reveal() {
    if (!active || limit >= selected.length) return;
    limit += 12;
    render();
  }
  function syncControls() {
    search.value = state.q;
    type.value = state.type;
    sort.value = state.sort;
  }
  function update() {
    state = parseFeedState(
      new URLSearchParams({
        q: search.value,
        type: type.value,
        sort: sort.value,
      }).toString(),
    );
    const query = serializeFeedState(state);
    if (query !== window.location.search)
      window.history.pushState(
        {},
        "",
        `${window.location.pathname}${query}${window.location.hash}`,
      );
    selected = selectPosts(posts, state);
    limit = 12;
    render();
  }
  const targetElement = (event: Event) =>
    event.target && "closest" in event.target
      ? (event.target as HTMLElement)
      : null;
  listen(root, "input", (event) => {
    if (event.target === search) update();
  });
  listen(root, "change", (event) => {
    if (event.target === type || event.target === sort) update();
  });
  listen(root, "submit", (event) => {
    if (event.target === controls) {
      event.preventDefault();
      update();
    }
  });
  listen(window, "popstate", () => {
    state = parseFeedState(window.location.search);
    selected = selectPosts(posts, state);
    limit = 12;
    syncControls();
    render();
  });
  listen(root, "click", (event) => {
    const button = targetElement(event)?.closest<HTMLButtonElement>("button");
    if (!button || !root.contains(button)) return;
    if (button === more) {
      reveal();
      return;
    }
    if (button.hasAttribute("data-caption-toggle")) {
      const expanded = button.getAttribute("aria-expanded") !== "true";
      button.setAttribute("aria-expanded", String(expanded));
      button.textContent = expanded ? "Show less" : "Show more";
      button
        .closest(".caption")!
        .querySelector("[data-caption-text]")!
        .classList.toggle("is-collapsed", !expanded);
    }
    const gallery = button.closest<HTMLElement>("[data-carousel]");
    if (gallery && button.matches("[data-carousel-prev], [data-carousel-next]"))
      navigate(gallery, button.hasAttribute("data-carousel-next") ? 1 : -1);
  });
  listen(
    root,
    "click",
    (event) => {
      const gallery = suppressClickGallery;
      suppressClickGallery = undefined;
      if (
        gallery &&
        (event as MouseEvent).detail > 0 &&
        gallery.contains(targetElement(event))
      ) {
        event.preventDefault();
        event.stopPropagation();
      }
    },
    true,
  );
  listen(root, "keydown", (event) => {
    const key = event as KeyboardEvent;
    if (key.key !== "ArrowLeft" && key.key !== "ArrowRight") return;
    const gallery =
      targetElement(event)?.closest<HTMLElement>("[data-carousel]");
    if (
      !gallery ||
      !gallery.contains(document.activeElement) ||
      key.altKey ||
      key.ctrlKey ||
      key.metaKey
    )
      return;
    key.preventDefault();
    navigate(gallery, key.key === "ArrowRight" ? 1 : -1);
  });
  listen(
    root,
    "scroll",
    (event) => {
      const track = targetElement(event);
      const gallery = track?.closest<HTMLElement>("[data-carousel]");
      if (
        track?.matches(".media-track") &&
        gallery &&
        Math.abs(
          track.scrollLeft - track.clientWidth * (positions.get(gallery) ?? 0),
        ) <= 1
      )
        navigating.delete(gallery);
      if (
        track?.matches(".media-track") &&
        gallery &&
        track.clientWidth &&
        !navigating.has(gallery)
      )
        setPosition(gallery, Math.round(track.scrollLeft / track.clientWidth));
    },
    true,
  );
  listen(
    root,
    "scrollend",
    (event) => {
      const track = targetElement(event);
      const gallery = track?.closest<HTMLElement>("[data-carousel]");
      if (track?.matches(".media-track") && gallery && track.clientWidth) {
        navigating.delete(gallery);
        setPosition(gallery, Math.round(track.scrollLeft / track.clientWidth));
      }
    },
    true,
  );
  function releaseGesture() {
    if (!gesture) return;
    const released = gesture;
    gesture = undefined;
    try {
      if (released.captured)
        released.gallery.releasePointerCapture?.(released.id);
    } catch {
      /* Browser may already have released a cancelled pointer. */
    }
  }
  listen(root, "pointerdown", (event) => {
    suppressClickGallery = undefined;
    releaseGesture();
    const pointer = event as PointerEvent;
    const target = targetElement(event);
    const gallery = target?.closest<HTMLElement>("[data-carousel]");
    if (
      !gallery ||
      pointer.button !== 0 ||
      pointer.isPrimary === false ||
      target?.closest("button, a, input")
    )
      return;
    const video = target?.closest<HTMLVideoElement>("video");
    if (video?.controls) {
      const bounds = video.getBoundingClientRect();
      // Native controls are retargeted to <video>; reserve their bottom strip
      // so seeking/volume drags are not mistaken for carousel gestures.
      if (bounds.height > 0 && pointer.clientY >= bounds.bottom - 64) return;
    }
    gesture = {
      gallery,
      id: pointer.pointerId,
      x: pointer.clientX,
      y: pointer.clientY,
      videoOrigin: !!video,
      captured: !video,
    };
    if (!video) gallery.setPointerCapture?.(pointer.pointerId);
  });
  listen(root, "pointermove", (event) => {
    const pointer = event as PointerEvent;
    if (!gesture?.videoOrigin || gesture.id !== pointer.pointerId) return;
    const dx = pointer.clientX - gesture.x;
    const dy = pointer.clientY - gesture.y;
    if (Math.abs(dx) >= 50 && Math.abs(dx) > Math.abs(dy)) {
      if (!gesture.captured) {
        gesture.gallery.setPointerCapture?.(gesture.id);
        gesture.captured = true;
      }
      event.preventDefault();
    }
  });
  listen(root, "pointerup", (event) => {
    const pointer = event as PointerEvent;
    if (!gesture || gesture.id !== pointer.pointerId) return;
    const dx = pointer.clientX - gesture.x;
    const dy = pointer.clientY - gesture.y;
    if (Math.abs(dx) >= 50 && Math.abs(dx) > Math.abs(dy)) {
      if (gesture.videoOrigin) {
        event.preventDefault();
        suppressClickGallery = gesture.gallery;
      }
      navigate(gesture.gallery, dx < 0 ? 1 : -1);
    }
    releaseGesture();
  });
  listen(root, "pointercancel", releaseGesture);
  listen(root, "lostpointercapture", (event) => {
    // Ignore loss of video's implicit capture when we transfer it to gallery.
    if (event.target === gesture?.gallery) releaseGesture();
  });
  syncControls();
  render();
  controls.hidden = false;
  controls
    .querySelectorAll<HTMLInputElement | HTMLSelectElement>("input, select")
    .forEach((control) => {
      control.disabled = false;
    });
  // Window's constructor properties are available in browsers but absent from TS's Window interface.
  const Observer = (
    window as Window & { IntersectionObserver?: typeof IntersectionObserver }
  ).IntersectionObserver;
  const observer = Observer
    ? new Observer(
        (entries) => {
          if (
            entries.some(
              (entry) => entry.target === sentinel && entry.isIntersecting,
            )
          )
            reveal();
        },
        { rootMargin: "300px" },
      )
    : undefined;
  observer?.observe(sentinel);
  return () => {
    active = false;
    observer?.disconnect();
    disposers.forEach((dispose) => dispose());
    releaseGesture();
    suppressClickGallery = undefined;
    controls.hidden = true;
    more.hidden = true;
    rows.forEach((row) => {
      row.querySelectorAll<HTMLButtonElement>("button").forEach((button) => {
        button.hidden = true;
      });
      row
        .querySelector("[data-caption-text]")
        ?.classList.remove("is-collapsed");
      const gallery = row.querySelector<HTMLElement>("[data-carousel]");
      if (gallery) delete gallery.dataset.enhanced;
    });
  };
}
