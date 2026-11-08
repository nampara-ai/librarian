//! Deterministic document rules. No filesystem, clock, network, or host bindings.
#![forbid(unsafe_code)]

pub mod corpus;
mod document;
pub mod editorial;
pub mod layout;
mod markdown;

pub use document::*;
pub use markdown::*;
