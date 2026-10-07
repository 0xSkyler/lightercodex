/* Optional browser acceptance test: requires Playwright and Chromium.
 * Runs an isolated test server with fake account/process adapters. Never connects to Lighter.
 */
const { chromium } = require("playwright");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const assert = require("node:assert/strict");
const root = fs.mkdtempSync(path.join(os.tmpdir(), "lighter-browser-"));
const child = spawn(
  process.env.DASHBOARD_PYTHON || ".venv/bin/python",
  ["tests/dashboard_browser_fixture.py", root],
  { stdio: ["ignore", "pipe", "pipe"] },
);
let browser,
  stderr = "";
child.stderr.on("data", (b) => (stderr += b.toString()));
async function main() {
  const url = await new Promise((resolve, reject) => {
    let buffer = "";
    const timeout = setTimeout(
      () => reject(new Error(`Fixture did not start: ${stderr}`)),
      15000,
    );
    child.stdout.on("data", (b) => {
      buffer += b.toString();
      const m = buffer.match(/http:\/\/127\.0\.0\.1:\d+/);
      if (m) {
        clearTimeout(timeout);
        resolve(m[0]);
      }
    });
    child.on("exit", (code) => {
      clearTimeout(timeout);
      reject(new Error(`Fixture exited ${code}: ${stderr}`));
    });
  });
  browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_PATH || "/usr/bin/chromium",
    headless: true,
    args: ["--no-sandbox"],
  });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1000 },
  });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  await page.goto(url);
  await page.locator("#start-bot:not([disabled])").waitFor();
  assert.equal(await page.locator("#bot-value").innerText(), "Stopped");
  assert.equal(await page.locator("#trades-empty").isVisible(), true);
  assert.equal(await page.evaluate(() => document.body.scrollWidth), 1440);
  await page.locator("[data-nav=credentials]").click();
  await page.waitForFunction(
    () =>
      location.hash === "#credentials" &&
      document
        .querySelector("[data-nav=credentials]")
        .classList.contains("active"),
  );
  assert.equal(
    await page
      .locator("[data-nav=overview]")
      .evaluate((el) => el.classList.contains("active")),
    false,
  );
  await page.locator("[name=LIGHTER_ACCOUNT_INDEX]").fill("123");
  await page.locator("[name=LIGHTER_API_KEY_INDEX]").fill("3");
  await page.locator("[name=LIGHTER_API_PRIVATE_KEY]").fill("a".repeat(80));
  const check = page.waitForResponse(
    (r) => r.url().endsWith("/api/account") && r.request().method() === "POST",
  );
  await page.locator("#verify-account").click();
  assert.equal((await check).status(), 200);
  await page.waitForFunction(() =>
    document.querySelector("#verified-balance").textContent.includes("37.50"),
  );
  assert.equal(
    await page.locator("[name=LIGHTER_API_PRIVATE_KEY]").inputValue(),
    "",
  );
  const publicResponse = await page.request.get(`${url}/api/bootstrap`);
  assert.equal((await publicResponse.text()).includes("a".repeat(80)), false);
  const saved = fs.readFileSync(path.join(root, "settings.env"), "utf8");
  assert.ok(saved.includes("a".repeat(80)));
  assert.equal(
    fs.statSync(path.join(root, "settings.env")).mode & 0o777,
    0o600,
  );
  await page.locator("#apply-account:not([disabled])").waitFor();
  await page.locator("#apply-account").click();
  await page.waitForFunction(
    () =>
      location.hash === "#strategy" &&
      !document.querySelector("[name=LEVERAGE]").disabled,
  );
  assert.equal(await page.locator("[name=TX_PER_MINUTE]").inputValue(), "30");
  await page.locator("[name=LEVERAGE]").selectOption("5");
  const save = page.waitForResponse(
    (r) => r.url().endsWith("/api/settings") && r.request().method() === "POST",
  );
  await page.locator("#strategy-form button[type=submit]").click();
  assert.equal((await save).status(), 200);
  await page.locator("#validate-settings:not([disabled])").waitFor();
  const valid = page.waitForResponse((r) => r.url().endsWith("/api/validate"));
  await page.locator("#validate-settings").click();
  assert.equal((await valid).status(), 200);
  await page.locator("[data-nav=overview]").click();
  await page.locator("#start-bot:not([disabled])").waitFor();
  await page.locator("#start-bot").click();
  await page.locator("#confirm-dialog").waitFor();
  assert.equal(await page.locator("#accept-confirm").isDisabled(), true);
  await page.locator("#confirmation-input").fill("START");
  assert.equal(await page.locator("#accept-confirm").isDisabled(), true);
  await page.locator("#cancel-confirm").click();
  assert.equal(await page.locator("#bot-value").innerText(), "Stopped");
  await page.locator("#start-bot").click();
  await page.locator("#confirmation-input").fill("START LIVE");
  const start = page.waitForResponse((r) => r.url().endsWith("/api/start"));
  await page.locator("#accept-confirm").click();
  assert.equal((await start).status(), 200);
  await page.locator("#stop-bot:not([disabled])").waitFor();
  await page.locator("[data-nav=credentials]").click();
  assert.equal(
    await page.locator("[name=LIGHTER_ACCOUNT_INDEX]").isDisabled(),
    true,
  );
  await page.locator("[data-nav=overview]").click();
  const stop = page.waitForResponse((r) => r.url().endsWith("/api/stop"));
  await page.locator("#stop-bot").click();
  assert.equal((await stop).status(), 200);
  await page.locator("#flatten-bot:not([disabled])").waitFor();
  await page.locator("#flatten-bot").click();
  await page.locator("#confirmation-input").fill("FLATTEN BTC");
  const flatten = page.waitForResponse((r) => r.url().endsWith("/api/flatten"));
  await page.locator("#accept-confirm").click();
  assert.equal((await flatten).status(), 200);
  await page.locator("[data-nav=activity]").click();
  await page.waitForFunction(() =>
    document
      .querySelector("#activity-log")
      .textContent.includes("LOCAL BROWSER FIXTURE"),
  );
  assert.equal(
    await page.evaluate(() => localStorage.length + sessionStorage.length),
    0,
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator("[data-nav=overview]").click();
  await page.waitForFunction(
    () => !document.querySelector("#view-overview").hidden,
  );
  assert.equal(await page.evaluate(() => document.body.scrollWidth), 390);
  await page.locator("[data-chart=pnl]").click();
  assert.equal(await page.locator("#chart-empty").isVisible(), true);
  await page.locator("[data-chart=price]").click();
  assert.equal(await page.locator("#chart-empty").isVisible(), false);
  assert.deepEqual(errors, []);
  console.log(
    "Browser acceptance passed: navigation, credential save/verification, key secrecy, strategy, typed confirmations, fake start/stop/flatten, journal, and mobile layout. No exchange requests or orders.",
  );
}
main()
  .catch((e) => {
    console.error(e);
    process.exitCode = 1;
  })
  .finally(async () => {
    if (browser) await browser.close();
    child.kill("SIGTERM");
  });
