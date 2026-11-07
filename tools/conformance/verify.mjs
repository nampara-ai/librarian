import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, extname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { fixtures, diagrams } from "./fixtures.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const native = spawnSync("cargo", ["run", "--quiet", "--locked", "-p", "librarian-app", "--example", "conformance"], {
  cwd: root, encoding: "utf8", input: fixtures.map(f => JSON.stringify(f.request)).join("\n") + "\n",
  maxBuffer: 16 * 1024 * 1024,
});
if (native.error) throw native.error;
assert.equal(native.status, 0, native.stderr);
const nativeResults = native.stdout.trim().split("\n").map(line => JSON.parse(line));
const require = createRequire(import.meta.url);
const wasm = require(resolve(root, "crates/librarian-wasm/pkg/librarian_wasm.js"));
const wasmResults = fixtures.map(f => JSON.parse(wasm.execute_json(JSON.stringify(f.request))));

function check(results, host) {
  assert.equal(results.length, fixtures.length);
  fixtures.forEach((fixture, i) => {
    const result = results[i];
    assert.equal(result.schema_version, 1, `${host}: ${fixture.name}`);
    if (fixture.error) {
      assert.equal(result.status, "error", `${host}: ${fixture.name}`);
      assert.equal(result.code, fixture.error, `${host}: ${fixture.name}`);
    } else {
      assert.equal(result.status, "ok", `${host}: ${fixture.name}`);
      assert.equal(result.result.markdown, fixture.markdown, `${host}: ${fixture.name}`);
      for (const [key, expected] of Object.entries(fixture.stats ?? {})) {
        assert.equal(result.result.stats[key], expected, `${host}: ${fixture.name}: ${key}`);
      }
    }
  });
}
check(nativeResults, "native");
check(wasmResults, "Node WASM");
assert.deepEqual(wasmResults, nativeResults, "native/Node WASM mismatch");

// Serve only the test harness, dependencies and generated web bundle on loopback.
const allowed = ["tools/conformance", "crates/librarian-wasm/pkg-web"].map(p => resolve(root, p) + sep);
const mime = { ".js": "text/javascript", ".mjs": "text/javascript", ".wasm": "application/wasm", ".json": "application/json" };
const server = createServer(async (request, response) => {
  try {
    const pathname = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
    if (pathname === "/") {
      response.setHeader("Content-Type", "text/html");
      response.end("<!doctype html><meta charset=utf-8><title>Librarian conformance</title><body></body>");
      return;
    }
    const path = resolve(root, "." + pathname);
    if (!allowed.some(prefix => path.startsWith(prefix))) { response.writeHead(403).end(); return; }
    response.setHeader("Content-Type", mime[extname(path)] ?? "application/octet-stream");
    response.end(await readFile(path));
  } catch { response.writeHead(404).end(); }
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
let browser;
try {
  browser = await chromium.launch({ headless: true,
    ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH } : {}),
  });
  const page = await browser.newPage();
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  const browserResults = await page.evaluate(requests => new Promise((resolve, reject) => {
    const worker = new Worker("/tools/conformance/worker.mjs", { type: "module" });
    const timeout = setTimeout(() => { worker.terminate(); reject(Error("WASM worker timed out")); }, 30000);
    const finish = () => { clearTimeout(timeout); worker.terminate(); };
    worker.onerror = event => { finish(); reject(Error(event.message)); };
    worker.onmessage = ({ data }) => { finish(); data.error ? reject(Error(data.error)) : resolve(data.results); };
    worker.postMessage(requests);
  }), fixtures.map(f => f.request));
  check(browserResults, "browser worker WASM");
  assert.deepEqual(browserResults, nativeResults, "native/browser WASM mismatch");
  await page.evaluate(async diagrams => {
    const pele = await import("/tools/conformance/node_modules/pele/dist/pele.js");
    const { default: mermaid } = await import("/tools/conformance/node_modules/mermaid/dist/mermaid.esm.min.mjs");
    mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });
    for (const [i, diagram] of diagrams.entries()) {
      pele.parse(diagram.source);
      await mermaid.parse(diagram.source);
      const rendered = [pele.render(diagram.source).svg, (await mermaid.render(`diagram-${i}`, diagram.source)).svg];
      for (const svg of rendered) {
        const doc = new DOMParser().parseFromString(svg, "image/svg+xml");
        if (!doc.querySelector("svg") || doc.querySelector("parsererror")) throw Error("Invalid SVG");
        const labels = [...doc.querySelectorAll("text, foreignObject")].map(node => node.textContent).join(" ");
        for (const label of diagram.labels) {
          if (!labels.includes(label)) throw Error(`Rendered label missing: ${label}`);
        }
      }
    }
  }, diagrams);
  console.log(`${fixtures.length} golden fixtures passed: native, Node WASM, browser worker WASM (${browser.version()}).`);
  console.log(`${diagrams.length} diagram samples parsed and rendered with Pele 1.13.0 and Mermaid.js 12.3.0; labels retained.`);
} finally {
  await browser?.close();
  await new Promise(resolve => server.close(resolve));
}
