import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile, symlink, rm, open } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

async function fixture(t, html = '<img src="/archive/media/SAMPLE/0.webp">') {
  const root = await mkdtemp(join(tmpdir(), "static-check-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  await mkdir(join(root, "archive/media/SAMPLE"), { recursive: true });
  await writeFile(join(root, "index.html"), html);
  await writeFile(join(root, "archive/media/SAMPLE/0.webp"), "image");
  return root;
}
function run(root, env = {}) {
  const result = spawnSync(
    process.execPath,
    ["scripts/check-static-output.mjs", "--root", root],
    { encoding: "utf8", env: { ...process.env, ASTRO_BASE_PATH: "", ...env } },
  );
  assert.ok(result.stdout.trim(), result.stderr);
  return { ...result, report: JSON.parse(result.stdout) };
}
test("accepts local output and reports exact Python CLI budget", async (t) => {
  const result = run(await fixture(t));
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.report.budget.level, "ok");
  assert.ok(result.report.budget.total_bytes > 5);
});
test("uses an explicit PYTHON interpreter and never silently falls back", async (t) => {
  const root = await fixture(t);
  const result = run(root, { PYTHON: join(root, "missing-python") });
  assert.notEqual(result.status, 0);
  assert.match(result.report.error, /Budget CLI failed/);
});
test("defaults to python on PATH without requiring uv", async (t) => {
  const interpreter = spawnSync(
    process.env.PYTHON || "python",
    ["-c", "import sys; print(sys.executable)"],
    { encoding: "utf8" },
  );
  assert.equal(interpreter.status, 0, interpreter.stderr);
  const root = await fixture(t);
  const bin = await mkdtemp(join(tmpdir(), "static-python-"));
  t.after(() => rm(bin, { recursive: true, force: true }));
  const executable = interpreter.stdout.trim();
  // A bare symlink outside a venv loses pyvenv.cfg discovery; exec the real interpreter.
  await writeFile(
    join(bin, "python"),
    `#!${executable}\nimport os, sys\nos.execv(${JSON.stringify(executable)}, [${JSON.stringify(executable)}, *sys.argv[1:]])\n`,
    { mode: 0o700 },
  );
  const result = run(root, { PYTHON: "", PATH: bin });
  assert.equal(result.status, 0, result.report.error);
  assert.equal(result.report.budget.level, "ok");
});
for (const base of ["/owner-repo", "/owner-repo/"])
  test(`accepts ${base} URLs mapped to unchanged artifact paths`, async (t) => {
    const root = await fixture(
      t,
      '<script src="/owner-repo/_astro/client.js"></script><link rel="stylesheet" href="/owner-repo/_astro/style.css"><img src="/owner-repo/archive/media/SAMPLE/0.webp" srcset="/owner-repo/archive/media/SAMPLE/0.webp 320w"><video poster="/owner-repo/archive/media/SAMPLE/0.webp"></video>',
    );
    await mkdir(join(root, "_astro"));
    await writeFile(join(root, "_astro/client.js"), "");
    await writeFile(
      join(root, "_astro/style.css"),
      '.x{background:url("/owner-repo/archive/media/SAMPLE/0.webp")}',
    );
    await writeFile(
      join(root, "archive/manifest.json"),
      JSON.stringify({ asset: { asset_path: "media/SAMPLE/0.webp" } }),
    );
    const result = run(root, { ASTRO_BASE_PATH: base });
    assert.equal(result.status, 0, result.report.error);
  });
test("rejects unprefixed root media for a project deployment", async (t) => {
  const result = run(await fixture(t), { ASTRO_BASE_PATH: "/owner-repo" });
  assert.notEqual(result.status, 0);
  assert.match(result.report.error, /base|reference/i);
});
for (const [name, html] of [
  ["missing asset", '<img src="/archive/media/missing.webp">'],
  [
    "missing srcset asset",
    '<img src="/archive/media/SAMPLE/0.webp" srcset="/archive/media/missing.webp 320w">',
  ],
  [
    "remote CDN media",
    '<video poster="https://scontent.cdninstagram.com/p.webp"><source src="https://video.cdninstagram.com/v.mp4"></video>',
  ],
  ["other remote media", '<img src="//example.com/image.webp">'],
  ["missing bundle", '<script src="/_astro/missing.js"></script>'],
  ["path escape", '<img src="/archive/%2e%2e/%2e%2e/private.webp">'],
  [
    "missing embedded late-post asset",
    '<script type="application/json" id="archive-data">{"posts":[{"media":[{"asset":{"asset_path":"media/missing.webp"}}]}]}</script>',
  ],
])
  test(`rejects ${name}`, async (t) => {
    const result = run(await fixture(t, html));
    assert.notEqual(result.status, 0);
    assert.match(result.report.error, /reference|remote|escape/i);
  });
test("rejects unexpected files", async (t) => {
  const root = await fixture(t);
  await writeFile(join(root, "session.json"), "secret");
  assert.match(run(root).report.error, /unexpected/i);
});
for (const directory of [false, true])
  test(`rejects ${directory ? "directory" : "file"} symlinks`, async (t) => {
    const root = await fixture(t);
    await symlink(
      directory ? "SAMPLE" : "SAMPLE/0.webp",
      join(root, "archive/media/link"),
    );
    assert.match(run(root).report.error, /symlink/i);
  });
async function sparse(path, bytes) {
  const handle = await open(path, "w");
  await handle.truncate(bytes);
  await handle.close();
}
test("uses Python per-file boundary: 95 MiB accepted, one byte above rejected", async (t) => {
  const root = await fixture(t);
  await sparse(join(root, "archive/media/SAMPLE/0.webp"), 95 * 1024 * 1024);
  assert.equal(run(root).status, 0);
  await sparse(join(root, "archive/media/SAMPLE/0.webp"), 95 * 1024 * 1024 + 1);
  const result = run(root);
  assert.notEqual(result.status, 0);
  assert.equal(result.report.budget.level, "reject");
});
test("uses Python aggregate boundary: 899999999 accepted, 900000000 rejected", async (t) => {
  const root = await fixture(t, "");
  await sparse(join(root, "archive/media/SAMPLE/0.webp"), 89_999_999);
  for (let i = 1; i < 10; i++)
    await sparse(join(root, `archive/media/SAMPLE/${i}.webp`), 90_000_000);
  const allowed = run(root);
  assert.equal(allowed.status, 0);
  assert.equal(allowed.report.budget.total_bytes, 899_999_999);
  assert.equal(allowed.report.budget.level, "warning");
  await sparse(join(root, "archive/media/SAMPLE/0.webp"), 90_000_000);
  const rejected = run(root);
  assert.notEqual(rejected.status, 0);
  assert.equal(rejected.report.budget.total_bytes, 900_000_000);
  assert.equal(rejected.report.budget.level, "reject");
});

test("rejects oversized metadata through the budget CLI before parsing it", async (t) => {
  const root = await fixture(t);
  await sparse(join(root, "archive/manifest.json"), 95 * 1024 * 1024 + 1);
  const result = run(root);
  assert.notEqual(result.status, 0);
  assert.equal(result.report.budget?.level, "reject");
});

for (const [name, html, css] of [
  [
    "inline style element remote media",
    "<style>.x{background:url(https://scontent.cdninstagram.com/p.webp)}</style>",
  ],
  [
    "inline style attribute missing media",
    '<div style="background:url(/archive/media/missing.webp)"></div>',
  ],
  [
    "inline quoted import remote stylesheet",
    '<style>@import "https://example.com/remote.css";</style>',
  ],
  [
    "inline quoted import missing stylesheet",
    "<style>@import '/_astro/missing.css';</style>",
  ],
  [
    "standalone quoted import remote stylesheet",
    '<link rel="stylesheet" href="/_astro/test.css">',
    '@import "https://example.com/remote.css";',
  ],
  [
    "standalone quoted import missing stylesheet",
    '<link rel="stylesheet" href="/_astro/test.css">',
    "@import 'missing.css' screen;",
  ],
])
  test(`rejects CSS ${name}`, async (t) => {
    const root = await fixture(t, html);
    if (css) {
      await mkdir(join(root, "_astro"));
      await writeFile(join(root, "_astro/test.css"), css);
    }
    const result = run(root);
    assert.notEqual(result.status, 0);
    assert.match(result.report.error, /reference|remote/i);
  });

test("accepts CSS local inline URLs and quoted imports relative to their stylesheet", async (t) => {
  const root = await fixture(
    t,
    '<style>@import "/_astro/test.css"; .x{background:url("/archive/media/SAMPLE/0.webp")}</style><div style="background:url(/archive/media/SAMPLE/0.webp)"></div>',
  );
  await mkdir(join(root, "_astro"));
  await writeFile(join(root, "_astro/test.css"), "@import 'other.css' screen;");
  await writeFile(
    join(root, "_astro/other.css"),
    '.x{background:url("/archive/media/SAMPLE/0.webp")}',
  );
  const result = run(root);
  assert.equal(result.status, 0, result.report.error);
});
