## Summary

- 

## Verification

Rust terminal program (`crates/`, `tools/`):

- [ ] `cargo fmt --all --check`
- [ ] `cargo clippy --workspace --all-targets --locked -- -D warnings`
- [ ] `cargo test --locked -p librarian-core -p librarian-native --lib --test persistence`

Legacy Python/Swift application (`src/`, `apps/`, `tests/`):

- [ ] `ruff check .`
- [ ] `pyright`
- [ ] `pytest`
- [ ] `python -m build`
- [ ] Docker build if deployment/runtime dependencies changed

## Data Safety

- [ ] No private documents, transcripts, secrets, provider logs, or sensitive eval outputs added.
- [ ] New fixtures are synthetic, public-domain, or explicitly approved for open-source use.

## Notes

-
