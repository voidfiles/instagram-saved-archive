import { lstat, readdir, readFile } from "node:fs/promises";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { JSDOM } from "jsdom";

const site = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const root = resolve(
  args.length === 2 && args[0] === "--root" ? args[1] : join(site, "dist"),
);
let budget;

async function check() {
  if (args.length && (args.length !== 2 || args[0] !== "--root"))
    throw new Error("Usage: check-static-output.mjs [--root directory]");
  // Reject symlink ancestors as well as entries before any content is read.
  for (let path = root; ; path = dirname(path)) {
    if ((await lstat(path)).isSymbolicLink())
      throw new Error(`Symlink is not allowed: ${path}`);
    if (path === dirname(path)) break;
  }
  const files = new Set();
  async function walk(directory) {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      const name = relative(root, path).split(sep).join("/");
      if (entry.isSymbolicLink())
        throw new Error(`Symlink is not allowed: ${name}`);
      if (entry.isDirectory()) await walk(path);
      else if (!entry.isFile())
        throw new Error(`Unexpected non-regular file: ${name}`);
      else {
        if (
          !/^(?:index\.html|_astro\/[\w.-]+\.(?:js|css)|archive\/(?:manifest|sync-state)\.json|archive\/media\/[A-Za-z0-9_-]+\/[\w.-]+\.(?:webp|mp4))$/.test(
            name,
          )
        )
          throw new Error(`Unexpected file: ${name}`);
        files.add(name);
      }
    }
  }
  await walk(root);
  if (!files.has("index.html"))
    throw new Error("Missing local reference: index.html");
  // Check sizes before loading HTML/JSON so oversized metadata cannot exhaust memory.
  verifyBudget();
  function reference(value, base = "index.html", media = false) {
    if (!value || value.startsWith("#")) return;
    if (/^(?:[a-z][a-z\d+.-]*:|\/\/)/i.test(value)) {
      if (media) throw new Error(`Remote media reference: ${value}`);
      return;
    }
    const decoded = decodeURIComponent(value.split(/[?#]/)[0]);
    if (
      decoded.includes("\\") ||
      decoded.includes("\0") ||
      decoded.split("/").includes("..")
    )
      throw new Error(`Escaping local reference: ${value}`);
    const path = decoded.startsWith("/")
      ? decoded.slice(1)
      : join(dirname(base), decoded).split(sep).join("/");
    if (!files.has(path === "" ? "index.html" : path))
      throw new Error(`Missing local reference: ${value}`);
  }
  function assets(value) {
    if (!value || typeof value !== "object") return;
    for (const [key, child] of Object.entries(value)) {
      if (key === "asset_path" && typeof child === "string")
        reference(`/archive/${child}`, "index.html", true);
      else assets(child);
    }
  }
  function cssReferences(css, base = "index.html") {
    // Imports may use a bare quoted string rather than url(...).
    const urls =
      /\burl\(\s*(?:"([^"]*)"|'([^']*)'|([^\s'"()]+))\s*\)|@import\s+(?:"([^"]*)"|'([^']*)')/gi;
    for (const match of css.matchAll(urls))
      reference(
        match.slice(1).find((value) => value !== undefined),
        base,
        true,
      );
  }
  const dom = new JSDOM(await readFile(join(root, "index.html"), "utf8"));
  try {
    for (const node of dom.window.document.querySelectorAll(
      "[src], [href], [poster], [srcset]",
    )) {
      for (const attr of ["src", "href", "poster"]) {
        const value = node.getAttribute(attr);
        if (value)
          reference(
            value,
            "index.html",
            attr !== "href" || node.tagName !== "A",
          );
      }
      for (const part of (node.getAttribute("srcset") || "").split(",")) {
        const url = part.trim().split(/\s+/)[0];
        if (url) reference(url, "index.html", true);
      }
    }
    const embedded = dom.window.document.getElementById("archive-data");
    if (embedded) assets(JSON.parse(embedded.textContent));
    for (const node of dom.window.document.querySelectorAll("style"))
      cssReferences(node.textContent);
    for (const node of dom.window.document.querySelectorAll("[style]"))
      cssReferences(node.getAttribute("style"));
  } finally {
    dom.window.close();
  }
  if (files.has("archive/manifest.json"))
    assets(
      JSON.parse(await readFile(join(root, "archive/manifest.json"), "utf8")),
    );
  for (const file of files) {
    if (!file.endsWith(".css")) continue;
    cssReferences(await readFile(join(root, file), "utf8"), file);
  }
  // The Python command owns both thresholds; do not duplicate budget arithmetic here.
  function verifyBudget() {
    const result = spawnSync(
      "uv",
      ["run", "python", "-m", "sync.cli", "check-budget", "--root", root],
      {
        cwd: resolve(site, ".."),
        encoding: "utf8",
        env: { ...process.env, UV_OFFLINE: "1" },
      },
    );
    if (result.error)
      throw new Error(`Budget CLI failed: ${result.error.message}`);
    let report;
    try {
      report = JSON.parse(result.stdout);
    } catch {
      throw new Error("Budget CLI did not return JSON");
    }
    budget = report.budget;
    if (
      report.command !== "check-budget" ||
      !budget ||
      !["ok", "warning", "reject"].includes(budget.level) ||
      !Number.isSafeInteger(budget.total_bytes) ||
      budget.total_bytes < 0 ||
      !Array.isArray(budget.largest)
    )
      throw new Error("Budget CLI returned an invalid result");
    if (
      result.status !== 0 ||
      report.exit_code !== 0 ||
      report.status !== "ok" ||
      budget.level === "reject"
    )
      throw new Error("Publication size budget rejected the output");
  }
  return { status: "ok", files: files.size, budget };
}

try {
  console.log(JSON.stringify(await check()));
} catch (error) {
  console.log(
    JSON.stringify({
      status: "error",
      error: error.message,
      ...(budget ? { budget } : {}),
    }),
  );
  process.exitCode = 1;
}
