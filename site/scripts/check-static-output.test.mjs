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
function run(root) {
  const result = spawnSync(
    process.execPath,
    ["scripts/check-static-output.mjs", "--root", root],
    { encoding: "utf8" },
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
