import { lstatSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";

export interface AssetRecord {
  asset_path: string;
  mime_type: string;
  width: number;
  height: number;
  byte_size: number;
  sha256: string;
}

export interface MediaRecord {
  position: number;
  kind: "image" | "video";
  asset: AssetRecord;
  preview: AssetRecord;
  duration_seconds?: number;
}

export interface ArchivePost {
  shortcode: string;
  creator_username: string;
  creator_id: number;
  source_url: string;
  caption: string;
  published_at: string;
  archived_at: string;
  verified_at: string;
  media_type: "image" | "video" | "carousel";
  publication_state: "published";
  unpublished_reason: null;
  media: MediaRecord[];
}

export interface SyncState {
  schema_version: 1;
  backfill_complete: boolean;
  last_successful_sync_at: string | null;
  last_complete_saved_feed_scan_at: string | null;
  reconciliation_cursor: number;
  consecutive_known_threshold: number;
  archive_byte_size: number;
}

export interface ArchiveData {
  schema_version: 1;
  title: string;
  posts: ArchivePost[];
  sync_state: SyncState;
}

function requireValue(condition: unknown, context: string): asserts condition {
  if (!condition) throw new Error(`Invalid archive ${context}`);
}

function object(
  value: unknown,
  keys: string[],
  context: string,
  optional: string[] = [],
): Record<string, unknown> {
  requireValue(
    value !== null && typeof value === "object" && !Array.isArray(value),
    context,
  );
  const record = value as Record<string, unknown>;
  requireValue(
    Object.keys(record).every((key) => keys.includes(key)) &&
      keys.every((key) => key in record || optional.includes(key)),
    `${context} schema fields`,
  );
  return record;
}

function string(value: unknown, context: string, allowEmpty = false): string {
  requireValue(
    typeof value === "string" && (allowEmpty || value.length > 0),
    context,
  );
  return value;
}

function integer(value: unknown, context: string, minimum = 0): number {
  requireValue(
    typeof value === "number" &&
      Number.isSafeInteger(value) &&
      value >= minimum,
    context,
  );
  return value;
}

function timestamp(value: unknown, context: string): string {
  const text = string(value, context);
  requireValue(
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/.test(text) &&
      Number.isFinite(Date.parse(text)) &&
      new Date(text).toISOString().slice(0, 19) === text.slice(0, 19),
    context,
  );
  return text;
}

function assetPath(value: unknown): string {
  const path = string(value, "asset path");
  requireValue(
    !path.includes("\\") &&
      !path.includes(":") &&
      !path.includes("\0") &&
      path
        .split("/")
        .every((part) => part !== "" && part !== "." && part !== ".."),
    "asset path must stay beneath archive root",
  );
  return path;
}

export { assetUrl } from "../lib/public-path";

function readAsset(
  value: unknown,
  root: string,
  paths: Set<string>,
): AssetRecord {
  const data = object(
    value,
    ["asset_path", "mime_type", "width", "height", "byte_size", "sha256"],
    "asset",
  );
  const path = assetPath(data.asset_path);
  requireValue(!paths.has(path), "duplicate asset path");
  paths.add(path);
  let current = root;
  try {
    for (const part of path.split("/")) {
      current = join(current, part);
      requireValue(!lstatSync(current).isSymbolicLink(), "asset path symlink");
    }
    requireValue(lstatSync(current).isFile(), "asset must be a local file");
  } catch {
    throw new Error(`Missing or unsafe archive asset: ${path}`);
  }
  const byte_size = integer(data.byte_size, "asset byte_size", 1);
  requireValue(byte_size <= 95 * 1024 * 1024, "asset byte_size limit");
  const sha256 = string(data.sha256, "asset sha256");
  requireValue(/^[0-9a-f]{64}$/.test(sha256), "asset sha256");
  return {
    asset_path: path,
    mime_type: string(data.mime_type, "asset mime_type"),
    width: integer(data.width, "asset width", 1),
    height: integer(data.height, "asset height", 1),
    byte_size,
    sha256,
  };
}

function readMedia(
  value: unknown,
  root: string,
  paths: Set<string>,
): MediaRecord {
  const data = object(
    value,
    ["position", "kind", "asset", "preview", "duration_seconds"],
    "media",
    ["duration_seconds"],
  );
  requireValue(data.kind === "image" || data.kind === "video", "media kind");
  const asset = readAsset(data.asset, root, paths);
  const preview = readAsset(data.preview, root, paths);
  requireValue(preview.mime_type === "image/webp", "preview mime_type");
  const media: MediaRecord = {
    position: integer(data.position, "media position"),
    kind: data.kind,
    asset,
    preview,
  };
  if (data.kind === "video") {
    requireValue(
      typeof data.duration_seconds === "number" &&
        Number.isFinite(data.duration_seconds) &&
        data.duration_seconds > 0,
      "video duration_seconds",
    );
    media.duration_seconds = data.duration_seconds;
  } else {
    requireValue(!("duration_seconds" in data), "image duration_seconds");
  }
  return media;
}

function readPost(
  value: unknown,
  root: string,
  paths: Set<string>,
): ArchivePost {
  const data = object(
    value,
    [
      "shortcode",
      "creator_username",
      "creator_id",
      "source_url",
      "caption",
      "published_at",
      "archived_at",
      "verified_at",
      "media_type",
      "publication_state",
      "unpublished_reason",
      "media",
    ],
    "post",
  );
  const shortcode = string(data.shortcode, "shortcode");
  requireValue(/^[A-Za-z0-9_-]+$/.test(shortcode), "shortcode");
  const source_url = string(data.source_url, "source_url");
  const source = new URL(source_url);
  requireValue(source.protocol === "https:" && source.hostname, "source_url");
  requireValue(
    data.publication_state === "published" && data.unpublished_reason === null,
    "publication state",
  );
  requireValue(
    data.media_type === "image" ||
      data.media_type === "video" ||
      data.media_type === "carousel",
    "media_type",
  );
  requireValue(
    Array.isArray(data.media) && data.media.length > 0,
    "post media",
  );
  const media = data.media
    .map((item) => readMedia(item, root, paths))
    .sort((a, b) => a.position - b.position);
  requireValue(
    media.every((item, index) => item.position === index),
    "media positions",
  );
  requireValue(
    data.media_type === "carousel"
      ? media.length >= 2
      : media.length === 1 && media[0].kind === data.media_type,
    "media_type cardinality",
  );
  return {
    shortcode,
    creator_username: string(data.creator_username, "creator_username"),
    creator_id: integer(data.creator_id, "creator_id", 1),
    source_url,
    caption: string(data.caption, "caption", true)
      .replace(/\r\n?/g, "\n")
      .normalize("NFC"),
    published_at: timestamp(data.published_at, "published_at"),
    archived_at: timestamp(data.archived_at, "archived_at"),
    verified_at: timestamp(data.verified_at, "verified_at"),
    media_type: data.media_type,
    publication_state: "published",
    unpublished_reason: null,
    media,
  };
}

function readState(value: unknown): SyncState {
  const data = object(
    value,
    [
      "schema_version",
      "backfill_complete",
      "last_successful_sync_at",
      "last_complete_saved_feed_scan_at",
      "reconciliation_cursor",
      "consecutive_known_threshold",
      "archive_byte_size",
    ],
    "sync state",
  );
  requireValue(data.schema_version === 1, "sync state schema version");
  requireValue(
    typeof data.backfill_complete === "boolean",
    "backfill_complete",
  );
  return {
    schema_version: 1,
    backfill_complete: data.backfill_complete,
    last_successful_sync_at:
      data.last_successful_sync_at === null
        ? null
        : timestamp(data.last_successful_sync_at, "last_successful_sync_at"),
    last_complete_saved_feed_scan_at:
      data.last_complete_saved_feed_scan_at === null
        ? null
        : timestamp(
            data.last_complete_saved_feed_scan_at,
            "last_complete_saved_feed_scan_at",
          ),
    reconciliation_cursor: integer(
      data.reconciliation_cursor,
      "reconciliation_cursor",
    ),
    consecutive_known_threshold: integer(
      data.consecutive_known_threshold,
      "consecutive_known_threshold",
      1,
    ),
    archive_byte_size: integer(data.archive_byte_size, "archive_byte_size"),
  };
}

/** Build-time only. Byte integrity is validated by prepare-site-input before this boundary. */
export function loadArchive(root = resolve("public/archive")): ArchiveData {
  const manifest = object(
    JSON.parse(readFileSync(join(root, "manifest.json"), "utf8")),
    ["schema_version", "posts"],
    "manifest",
  );
  requireValue(manifest.schema_version === 1, "manifest schema version");
  requireValue(Array.isArray(manifest.posts), "manifest posts");
  const paths = new Set<string>();
  const posts = manifest.posts.map((post) => readPost(post, root, paths));
  requireValue(
    new Set(posts.map((post) => post.shortcode)).size === posts.length,
    "duplicate shortcode",
  );
  posts.sort((a, b) => {
    // Python timestamps retain microseconds, which Date.parse truncates.
    const key = (value: string) => {
      const [seconds, fraction = ""] = value.slice(0, -1).split(".");
      return `${seconds}.${fraction.padEnd(6, "0")}`;
    };
    const left = key(a.archived_at);
    const right = key(b.archived_at);
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
  const sync_state = readState(
    JSON.parse(readFileSync(join(root, "sync-state.json"), "utf8")),
  );
  return {
    schema_version: 1,
    title: process.env.SITE_TITLE?.trim() || "Saved",
    posts,
    sync_state,
  };
}

/** Serialize inline application/json without allowing HTML parser breakout. */
export function scriptSafeJson(value: unknown): string {
  const escaped: Record<string, string> = {
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
    "\u2028": "\\u2028",
    "\u2029": "\\u2029",
  };
  const json = JSON.stringify(value);
  if (json === undefined) throw new TypeError("Value is not JSON serializable");
  return json.replace(/[<>&\u2028\u2029]/g, (character) => escaped[character]);
}
