use wasm_bindgen::prelude::*;

/// The same versioned application boundary used by the native conformance runner.
#[wasm_bindgen]
pub fn execute_json(request: &str) -> String {
    librarian_app::execute_json(request)
}
