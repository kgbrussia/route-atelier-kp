// Печать КП в PDF через Chrome без окна: A4, с фоном, номерами страниц и печатной вёрсткой страницы КП.
// Запуск: node tools/kp-to-pdf.mjs <url или путь к .html> <out.pdf> [ещё пары...]
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const CHROME = process.env.CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const args = process.argv.slice(2);
if (args.length < 2 || args.length % 2) {
  console.error("usage: node tools/kp-to-pdf.mjs <url|file.html> <out.pdf> [...]");
  process.exit(1);
}

const port = 9300 + Math.floor(Math.random() * 500);
const profile = mkdtempSync(join(tmpdir(), "kp-pdf-"));
const chrome = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--no-first-run", "--no-default-browser-check", "--hide-scrollbars", "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function pageSocket() {
  for (let i = 0; i < 50; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" });
      if (r.ok) return (await r.json()).webSocketDebuggerUrl;
    } catch {}
    await sleep(200);
  }
  throw new Error("Chrome не запустился");
}

function client(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let id = 0;
  const pending = new Map();
  const waiters = [];
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { ok, fail } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? fail(new Error(msg.error.message)) : ok(msg.result);
    } else if (msg.method) {
      waiters.filter((w) => w.method === msg.method).forEach((w) => w.ok(msg.params));
    }
  };
  return {
    ready: new Promise((ok) => (ws.onopen = ok)),
    send: (method, params = {}) => new Promise((ok, fail) => {
      pending.set(++id, { ok, fail });
      ws.send(JSON.stringify({ id, method, params }));
    }),
    once: (method) => new Promise((ok) => waiters.push({ method, ok })),
    close: () => ws.close(),
  };
}

const footer = '<div style="font-size:8px;color:#8a94a3;width:100%;text-align:center;font-family:sans-serif">' +
  '<span class="pageNumber"></span> / <span class="totalPages"></span></div>';

try {
  const cdp = client(await pageSocket());
  await cdp.ready;
  await cdp.send("Page.enable");
  for (let i = 0; i < args.length; i += 2) {
    const src = /^https?:/.test(args[i]) ? args[i] : pathToFileURL(resolve(args[i])).href;
    const loaded = cdp.once("Page.loadEventFired");
    await cdp.send("Page.navigate", { url: src });
    await loaded;
    await cdp.send("Runtime.evaluate", { expression: "document.fonts.ready", awaitPromise: true });
    await sleep(500);
    const { data } = await cdp.send("Page.printToPDF", {
      printBackground: true, preferCSSPageSize: true, displayHeaderFooter: true,
      headerTemplate: "<span></span>", footerTemplate: footer,
    });
    writeFileSync(args[i + 1], Buffer.from(data, "base64"));
    console.log("ok", args[i + 1]);
  }
  cdp.close();
} finally {
  chrome.kill();
  await sleep(300);
  rmSync(profile, { recursive: true, force: true });
}
