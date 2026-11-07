//! Deterministic document rules. No filesystem, clock, network, or host bindings.
#![forbid(unsafe_code)]

pub mod corpus;
mod document;
mod markdown;

pub use document::*;
pub use markdown::*;
