use wasm_bindgen::prelude::*;

/// Portable document assembly, corpus rendering, chunk planning and fidelity rules.
/// Filesystem, OCR runtimes, credentials and provider transports belong to the host.
#[wasm_bindgen]
pub fn execute_json(request: &str) -> String {
    librarian_app::execute_json(request)
}
