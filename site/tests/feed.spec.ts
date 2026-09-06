import { expect, test, type Locator, type Page } from "@playwright/test";

test.beforeEach(async ({ context, page, baseURL }) => {
  await context.routeWebSocket("**/*", (socket) => socket.close());
  await context.route("**/*", (route) =>
    new URL(route.request().url()).origin === baseURL
      ? route.continue()
      : route.abort("blockedbyclient"),
  );
  await page.goto("/");
  await expect(page.getByRole("searchbox")).toBeEnabled();
});

test("one contained card per row, including portrait and tall media", async ({
  page,
}) => {
  const layout = await page.evaluate(() => {
    const cards = [...document.querySelectorAll(".post-row")].map((el) =>
      el.getBoundingClientRect(),
    );
    const media = [
      ...document.querySelectorAll<HTMLImageElement | HTMLVideoElement>(
        ".media-slide img, .media-slide video",
      ),
    ];
    return {
      viewport: innerWidth,
      width: document.documentElement.scrollWidth,
      feed: document.querySelector(".feed")!.getBoundingClientRect().width,
      onePerRow: cards.every(
        (card, i) => !i || card.top >= cards[i - 1].bottom,
      ),
      contained: media.every(
        (el) =>
          el.getBoundingClientRect().width <=
            el.closest(".media-track")!.clientWidth &&
          el.getBoundingClientRect().height <=
            Math.min(innerHeight * 0.82, 960) + 1,
      ),
      portrait: media.some(
        (el) =>
          Number(el.getAttribute("height")) > Number(el.getAttribute("width")),
      ),
      tall: media.some(
        (el) =>
          Number(el.getAttribute("height")) >=
          Number(el.getAttribute("width")) * 2,
      ),
    };
  });
  expect(layout.width).toBe(layout.viewport);
  expect(layout.feed).toBeLessThanOrEqual(760);
  expect(layout.onePerRow).toBe(true);
  expect(layout.contained).toBe(true);
  expect(layout.portrait).toBe(true);
  expect(layout.tall).toBe(true);
  const tall = page.locator('[data-shortcode="SAMPLE0003"] img');
  await tall.scrollIntoViewIfNeeded();
  await expect
    .poll(() =>
      tall.evaluate(
        (el: HTMLImageElement) => el.naturalHeight / el.naturalWidth,
      ),
    )
    .toBe(3);
  expect(await tall.evaluate((el) => getComputedStyle(el).objectFit)).toBe(
    "contain",
  );
});

test("accessible keyboard entry, attribution, lazy images and native video", async ({
  page,
}) => {
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: /skip/i })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#posts")).toBeFocused();
  const rows = page.locator(".post-row");
  for (const row of await rows.all()) {
    await expect(row.locator("h2 a")).toHaveAttribute(
      "href",
      /^https:\/\/www\.instagram\.com\/[^/]+\/$/,
    );
    await expect(row.locator(".source-link")).toHaveAttribute(
      "href",
      /^https:\/\/www\.instagram\.com\/p\/SAMPLE\d+\/$/,
    );
    await expect(row.locator(".source-link")).toHaveAttribute(
      "rel",
      "noopener noreferrer",
    );
  }
  for (const img of await page.locator(".media-slide img").all()) {
    await expect(img).toHaveAttribute("loading", "lazy");
    await expect(img).toHaveAttribute("decoding", "async");
    await expect(img).toHaveAttribute("alt", /Saved image/);
  }
  for (const video of await page.locator("video").all()) {
    expect(
      await video.evaluate((el: HTMLVideoElement) => ({
        controls: el.controls,
        autoplay: el.autoplay,
        paused: el.paused,
        preload: el.preload,
      })),
    ).toEqual({
      controls: true,
      autoplay: false,
      paused: true,
      preload: "metadata",
    });
  }
});

test("every filter, both sorts, creator and Unicode caption search", async ({
  page,
}) => {
  for (const type of ["image", "video", "carousel"]) {
    await page.getByLabel("Media type").selectOption(type);
    await expect(page.locator(".post-row")).toHaveCount(5);
    for (const row of await page.locator(".post-row").all())
      await expect(row).toHaveAttribute("data-media-type", type);
  }
  await page.getByLabel("Media type").selectOption("all");
  await page.getByLabel("Sort by").selectOption("published");
  await expect(page.locator(".post-row").first()).toHaveAttribute(
    "data-shortcode",
    "SAMPLE0014",
  );
  await page.getByLabel("Sort by").selectOption("archived");
  await expect(page.locator(".post-row").first()).toHaveAttribute(
    "data-shortcode",
    "SAMPLE0000",
  );
  await page.getByRole("searchbox").fill("FIELD.NOTES");
  await expect(page.locator(".post-row")).toHaveCount(5);
  await page.getByRole("searchbox").fill("日本語");
  await expect(page.locator(".post-row")).toHaveCount(12);
  await page.getByRole("searchbox").fill("no-match-for-this");
  await expect(page.getByRole("status")).toHaveText(
    "No saved posts match your filters.",
  );
  await expect(page.locator(".post-row")).toHaveCount(0);
});

test("reload and history restore query, controls and results", async ({
  page,
}) => {
  await page.getByRole("searchbox").fill("study");
  await page.getByLabel("Media type").selectOption("carousel");
  await page.getByLabel("Sort by").selectOption("published");
  await expect(page).toHaveURL(/\?q=study&type=carousel&sort=published$/);
  await page.reload();
  await expect(page.getByRole("searchbox")).toHaveValue("study");
  await expect(page.getByLabel("Media type")).toHaveValue("carousel");
  await expect(page.getByLabel("Sort by")).toHaveValue("published");
  await expect(page.locator(".post-row").first()).toHaveAttribute(
    "data-shortcode",
    "SAMPLE0014",
  );
  await page.getByLabel("Media type").selectOption("image");
  await page.goBack();
  await expect(page.getByLabel("Media type")).toHaveValue("carousel");
  await expect(page.locator(".post-row")).toHaveCount(5);
  await page.goForward();
  await expect(page.getByLabel("Media type")).toHaveValue("image");
  await expect(page.locator(".post-row").first()).toHaveAttribute(
    "data-shortcode",
    "SAMPLE0012",
  );
});

test("caption expands and collapses with keyboard and preserves focus", async ({
  page,
}) => {
  const button = page.locator(
    '[data-shortcode="SAMPLE0002"] [data-caption-toggle]',
  );
  await button.focus();
  const caption = page.locator(
    '[data-shortcode="SAMPLE0002"] [data-caption-text]',
  );
  const collapsedHeight = (await caption.boundingBox())!.height;
  await expect(button).toHaveAttribute("aria-expanded", "false");
  await page.keyboard.press("Enter");
  await expect(button).toHaveAttribute("aria-expanded", "true");
  await expect(button).toBeFocused();
  expect((await caption.boundingBox())!.height).toBeGreaterThan(
    collapsedHeight,
  );
  await page.keyboard.press("Space");
  await expect(button).toHaveAttribute("aria-expanded", "false");
  expect((await caption.boundingBox())!.height).toBe(collapsedHeight);
});

async function settled(gallery: Locator, index: number) {
  await expect(gallery.locator("[data-carousel-status]")).toHaveText(
    `${index + 1} of 3`,
  );
  await expect
    .poll(() =>
      gallery
        .locator(".media-track")
        .evaluate(
          (el, position) => Math.abs(el.scrollLeft - el.clientWidth * position),
          index,
        ),
    )
    .toBeLessThan(2);
}

test("carousel click and descendant keyboard focus scroll the track", async ({
  page,
}) => {
  const gallery = page.locator("[data-carousel]").first();
  const next = gallery.getByRole("button", { name: "Next slide" });
  await next.click();
  await settled(gallery, 1);
  await expect(next).toBeFocused();
  await page.keyboard.press("ArrowRight");
  await settled(gallery, 2);
  await expect(next).toBeFocused();
  await expect(next).toHaveAttribute("aria-disabled", "true");
  await page.keyboard.press("ArrowLeft");
  await settled(gallery, 1);
  await gallery.locator("video").focus();
  await page.keyboard.press("ArrowLeft");
  await settled(gallery, 0);
});

test("rapid navigation retains destination and status throughout smooth scroll", async ({
  page,
}) => {
  const gallery = page.locator("[data-carousel]").first();
  await gallery.scrollIntoViewIfNeeded();
  await gallery.getByRole("button", { name: "Next slide" }).focus();
  await page.keyboard.press("ArrowRight");
  // Wait for a real intermediate scroll frame, then navigate again before settling.
  await expect
    .poll(
      () =>
        gallery
          .locator(".media-track")
          .evaluate(
            (el) => el.scrollLeft > 0 && el.scrollLeft < el.clientWidth * 0.5,
          ),
      { intervals: [10] },
    )
    .toBe(true);
  expect(await gallery.locator("[data-carousel-status]").textContent()).toBe(
    "2 of 3",
  );
  await page.keyboard.press("ArrowRight");
  await settled(gallery, 2);
});

test("reduced motion navigates immediately and preserves subsequent destination and status", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const gallery = page.locator("[data-carousel]").first();
  await gallery.scrollIntoViewIfNeeded();
  // Capture offsets in the same browser task as each real controller click.
  // Waiting with locator assertions would let an unwanted animation finish.
  const samples = await gallery.evaluate((el) => {
    const track = el.querySelector<HTMLElement>(".media-track")!;
    const next = el.querySelector<HTMLButtonElement>("[data-carousel-next]")!;
    const prev = el.querySelector<HTMLButtonElement>("[data-carousel-prev]")!;
    return [next, next, prev].map((button) => {
      button.click();
      return {
        position: track.scrollLeft / track.clientWidth,
        status: el.querySelector("[data-carousel-status]")!.textContent,
      };
    });
  });
  expect(samples).toEqual([
    { position: 1, status: "2 of 3" },
    { position: 2, status: "3 of 3" },
    { position: 1, status: "2 of 3" },
  ]);
  await settled(gallery, 1);
  await gallery.getByRole("button", { name: "Next slide" }).focus();
  await page.keyboard.press("ArrowRight");
  await settled(gallery, 2);
});

async function touchSwipe(page: Page, target: Locator, controlBar = false) {
  await target.scrollIntoViewIfNeeded();
  const box = (await target.boundingBox())!;
  const y = controlBar ? box.y + box.height - 30 : box.y + box.height / 2;
  const client = await page.context().newCDPSession(page);
  const x = box.x + box.width * 0.8;
  await client.send("Input.dispatchTouchEvent", {
    type: "touchStart",
    touchPoints: [{ x, y }],
  });
  for (let i = 1; i <= 6; i++)
    await client.send("Input.dispatchTouchEvent", {
      type: "touchMove",
      touchPoints: [{ x: x - (box.width * 0.6 * i) / 6, y }],
    });
  await client.send("Input.dispatchTouchEvent", {
    type: "touchEnd",
    touchPoints: [],
  });
  await client.detach();
}

test("image touch swipe navigates carousel", async ({ page }) => {
  const gallery = page.locator("[data-carousel]").first();
  await touchSwipe(page, gallery.locator("img").first());
  await settled(gallery, 1);
});

test("native video control taps and control-bar drags survive; surface swipe navigates", async ({
  page,
}) => {
  const gallery = page.locator("[data-carousel]").first();
  await gallery.getByRole("button", { name: "Next slide" }).click();
  await settled(gallery, 1);
  const video = gallery.locator("video");
  await video.scrollIntoViewIfNeeded();
  await video.evaluate((el: HTMLVideoElement) => {
    el.loop = true;
  });
  const box = (await video.boundingBox())!;
  await video.evaluate((el) => {
    document.addEventListener(
      "pointerup",
      (event) => {
        setTimeout(() => {
          el.dataset.tapPrevented = String(event.defaultPrevented);
        }, 0);
      },
      { once: true, capture: true },
    );
  });
  await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2);
  await expect(video).toHaveAttribute("data-tap-prevented", "false");
  await settled(gallery, 1);
  await video.evaluate((el: HTMLVideoElement) => el.pause());
  // Chromium's native play button is in the bottom-left control strip.
  await page.touchscreen.tap(box.x + 20, box.y + box.height - 30);
  await expect
    .poll(() => video.evaluate((el: HTMLVideoElement) => el.paused))
    .toBe(false);
  await settled(gallery, 1);
  await touchSwipe(page, video, true);
  await settled(gallery, 1);
  await video.evaluate((el: HTMLVideoElement) => el.pause());
  await touchSwipe(page, video);
  await settled(gallery, 2);
  expect(await video.evaluate((el: HTMLVideoElement) => el.paused)).toBe(true);
});

test("progressively reveals 12 then all 15 cards", async ({ page }) => {
  await expect(page.locator(".post-row")).toHaveCount(12);
  await expect(page.getByRole("status")).toHaveText(
    "Showing 12 of 15 saved posts",
  );
  await page.locator("[data-feed-sentinel]").scrollIntoViewIfNeeded();
  await expect(page.locator(".post-row")).toHaveCount(15);
  await expect(page.getByRole("status")).toHaveText(
    "Showing 15 of 15 saved posts",
  );
  await expect(
    page.getByRole("button", { name: "Load more posts" }),
  ).toBeHidden();
});
