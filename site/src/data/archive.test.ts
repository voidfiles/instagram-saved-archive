import {
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
  unlinkSync,
  symlinkSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { loadArchive, scriptSafeJson, assetUrl } from "./archive";

let root: string;
const asset = (name: string) => ({
  asset_path: `media/${name}`,
  mime_type: "image/webp",
  width: 640,
  height: 480,
  byte_size: 1,
  sha256: "a".repeat(64),
});
const post = (shortcode: string, archived_at = "2026-09-01T12:00:00Z") => ({
  shortcode,
  creator_username: "field.notes",
  creator_id: 1,
  source_url: `https://www.instagram.com/p/${shortcode}/`,
  caption: "Café 日本語 🌿",
  published_at: "2026-08-01T12:00:00Z",
  archived_at,
  verified_at: "2026-09-01T12:00:00Z",
  media_type: "image",
  publication_state: "published",
  unpublished_reason: null,
  media: [
    {
      position: 0,
      kind: "image",
      asset: asset(`${shortcode}.webp`),
      preview: asset(`${shortcode}-preview.webp`),
    },
  ],
});
const writeManifest = (posts = [post("A")]) =>
  writeFileSync(
    join(root, "manifest.json"),
    JSON.stringify({ schema_version: 1, posts }),
  );
function change(file: string, mutate: (value: any) => void) {
  const path = join(root, file);
  const value = JSON.parse(readFileSync(path, "utf8"));
  mutate(value);
  writeFileSync(path, JSON.stringify(value));
}

beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "archive-loader-"));
  mkdirSync(join(root, "media"));
  for (const name of ["A", "B", "C"]) {
    writeFileSync(join(root, `media/${name}.webp`), "x");
    writeFileSync(join(root, `media/${name}-preview.webp`), "x");
  }
  writeManifest();
  writeFileSync(
    join(root, "sync-state.json"),
    JSON.stringify({
      schema_version: 1,
      backfill_complete: true,
      last_successful_sync_at: "2026-09-01T12:00:00Z",
      last_complete_saved_feed_scan_at: null,
      reconciliation_cursor: 0,
      consecutive_known_threshold: 20,
      archive_byte_size: 12,
    }),
  );
  vi.stubEnv("SITE_TITLE", "");
});
afterEach(() => {
  rmSync(root, { recursive: true, force: true });
  vi.unstubAllEnvs();
});

describe("loadArchive", () => {
  it("preserves Python microsecond ordering before shortcode tie-breaking", () => {
    writeManifest([
      post("A", "2026-09-01T12:00:00.000001Z"),
      post("B", "2026-09-01T12:00:00.000002Z"),
      post("C", "2026-09-01T12:00:00Z"),
    ]);
    expect(loadArchive(root).posts.map((item) => item.shortcode)).toEqual([
      "B",
      "A",
      "C",
    ]);
  });
  it("rejects impossible calendar dates", () => {
    change("manifest.json", (data) => {
      data.posts[0].published_at = "2026-02-30T12:00:00Z";
    });
    expect(() => loadArchive(root)).toThrow(/published_at/);
  });
  it("keeps Python JSON field names and defaults the configurable title", () => {
    const data = loadArchive(root);
    expect(data.title).toBe("Saved");
    expect(data.posts[0].creator_username).toBe("field.notes");
    expect(data.posts[0].caption).toBe("Café 日本語 🌿");
    expect(data.sync_state.last_successful_sync_at).toBe(
      "2026-09-01T12:00:00Z",
    );
    vi.stubEnv("SITE_TITLE", "Alex’s collection");
    expect(loadArchive(root).title).toBe("Alex’s collection");
  });
  it("orders newest archived first with shortcode ties", () => {
    writeManifest([post("C", "2026-08-01T12:00:00Z"), post("B"), post("A")]);
    expect(loadArchive(root).posts.map((item) => item.shortcode)).toEqual([
      "A",
      "B",
      "C",
    ]);
  });
  it.each(["manifest.json", "sync-state.json"])(
    "rejects unsupported versions in %s",
    (file) => {
      change(file, (data) => {
        data.schema_version = 2;
      });
      expect(() => loadArchive(root)).toThrow(/schema/i);
    },
  );
  it.each([
    [
      "manifest.json",
      (data: any) => {
        data.extra = true;
      },
    ],
    [
      "manifest.json",
      (data: any) => {
        delete data.posts[0].caption;
      },
    ],
    [
      "manifest.json",
      (data: any) => {
        data.posts[0].creator_id = "1";
      },
    ],
    [
      "manifest.json",
      (data: any) => {
        data.posts[0].publication_state = "private";
      },
    ],
    [
      "manifest.json",
      (data: any) => {
        data.posts[0].source_url = "javascript:alert(1)";
      },
    ],
    [
      "manifest.json",
      (data: any) => {
        data.posts[0].media[0].preview.extra = true;
      },
    ],
    [
      "manifest.json",
      (data: any) => {
        data.posts[0].media[0].position = 2;
      },
    ],
    [
      "manifest.json",
      (data: any) => {
        data.posts[0].media_type = "carousel";
      },
    ],
    [
      "manifest.json",
      (data: any) => {
        data.posts.push(data.posts[0]);
      },
    ],
    [
      "sync-state.json",
      (data: any) => {
        data.backfill_complete = "true";
      },
    ],
    [
      "sync-state.json",
      (data: any) => {
        data.last_successful_sync_at = "not-a-date";
      },
    ],
  ])("rejects invalid schema in %s", (file, mutate) => {
    change(file, mutate);
    expect(() => loadArchive(root)).toThrow();
  });
  it("fails when a local preview is missing", () => {
    unlinkSync(join(root, "media/A-preview.webp"));
    expect(() => loadArchive(root)).toThrow(/asset/i);
  });
  it("rejects symlinked local assets", () => {
    unlinkSync(join(root, "media/A.webp"));
    symlinkSync(join(root, "manifest.json"), join(root, "media/A.webp"));
    expect(() => loadArchive(root)).toThrow(/asset|symlink/i);
  });
  it.each([
    "../escape.webp",
    "/tmp/escape.webp",
    "https://example.com/a.webp",
    "media/../a.webp",
    "media\\a.webp",
  ])("rejects unsafe path %s", (path) => {
    change("manifest.json", (data) => {
      data.posts[0].media[0].asset.asset_path = path;
    });
    expect(() => loadArchive(root)).toThrow(/path|asset/i);
  });
  it("maps local paths to encoded URLs beneath /archive/", () => {
    expect(assetUrl("media/A.webp")).toBe("/archive/media/A.webp");
    expect(assetUrl("media/a #?.webp")).toBe("/archive/media/a%20%23%3F.webp");
    expect(() => assetUrl("../escape")).toThrow();
  });
});

describe("scriptSafeJson", () => {
  it("neutralizes script-closing captions", () => {
    const output = scriptSafeJson({
      caption: "</script><script>alert(1)</script>",
    });
    expect(output).not.toContain("</script>");
    expect(output).toContain("\\u003c/script");
  });
  it("escapes HTML-sensitive characters and Unicode separators while round-tripping", () => {
    const value = { caption: "<>&\u2028\u2029 café 🌿" };
    const output = scriptSafeJson(value);
    expect(output).not.toMatch(/[<>&\u2028\u2029]/u);
    expect(JSON.parse(output)).toEqual(value);
  });
});
