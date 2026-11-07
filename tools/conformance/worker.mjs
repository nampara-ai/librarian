import init, { execute_json } from "/crates/librarian-wasm/pkg-web/librarian_wasm.js";
const ready = init();
self.onmessage = async ({ data }) => {
  try {
    await ready;
    self.postMessage({ results: data.map(request => JSON.parse(execute_json(JSON.stringify(request)))) });
  } catch (error) { self.postMessage({ error: String(error) }); }
};
