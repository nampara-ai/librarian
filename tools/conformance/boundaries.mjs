import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const result = spawnSync("cargo", ["metadata", "--locked", "--format-version", "1", "--no-deps"], { cwd: root, encoding: "utf8" });
if (result.error) throw result.error;
assert.equal(result.status, 0, result.stderr);
const { packages } = JSON.parse(result.stdout);
// An allowlist makes a newly introduced host dependency a deliberate review event.
const allowed = {
  "librarian-core": ["serde", "thiserror", "pulldown-cmark", "blake3"],
  "librarian-app": ["librarian-core", "serde", "serde_json", "thiserror"],
  "librarian-wasm": ["librarian-app", "wasm-bindgen"],
};
for (const [name, dependencies] of Object.entries(allowed)) {
  const pkg = packages.find(pkg => pkg.name === name);
  assert(pkg, `${name} missing`);
  for (const dependency of pkg.dependencies.filter(d => d.kind !== "dev")) {
    assert(dependencies.includes(dependency.name), `${name} gained an unreviewed dependency: ${dependency.name}`);
  }
}
console.log("Shared-crate dependency boundaries passed.");
