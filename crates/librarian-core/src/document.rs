use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// The initial implementation processes bounded batches, not an entire corpus.
pub const MAX_SOURCE_BYTES: usize = 16 * 1024 * 1024;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceText {
    /// An occurrence identity, not a content hash: identical pages remain distinct.
    pub id: String,
    pub text: String,
}

/// Half-open UTF-8 byte coordinates in a bounded source observation.
/// u32 has identical semantics in native/WASM and is exact in JavaScript numbers.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceSpan {
    pub source: String,
    pub start: u32,
    pub end: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Join {
    #[default]
    None,
    Space,
    LineBreak,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Fragment {
    pub span: SourceSpan,
    #[serde(default)]
    pub join_before: Join,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum BlockKind {
    Paragraph,
    Heading {
        level: u8,
    },
    Code {
        language: String,
    },
    DisplayMath,
    /// Existing Markdown is preserved, never flattened and guessed back into shape.
    Markdown,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Block {
    pub id: String,
    pub kind: BlockKind,
    pub fragments: Vec<Fragment>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExclusionReason {
    Whitespace,
    PageFurniture,
    LineWrapHyphen,
    UserPolicy,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExcludedSpan {
    pub span: SourceSpan,
    pub reason: ExclusionReason,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Document {
    pub sources: Vec<SourceText>,
    pub blocks: Vec<Block>,
    #[serde(default)]
    pub exclusions: Vec<ExcludedSpan>,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum DocumentError {
    #[error("Source batch exceeds the {MAX_SOURCE_BYTES}-byte limit")]
    TooLarge,
    #[error("Empty or duplicate {kind} identity: {id:?}")]
    Identity { kind: &'static str, id: String },
    #[error("Unknown source: {0}")]
    UnknownSource(String),
    #[error("Invalid UTF-8 byte span in {source_id}: {start}..{end}")]
    InvalidSpan {
        source_id: String,
        start: u32,
        end: u32,
    },
    #[error("Unaccounted source bytes in {source_id}: {start}..{end}")]
    Missing {
        source_id: String,
        start: u32,
        end: u32,
    },
    #[error("Overlapping source ownership in {source_id} at byte {at}")]
    Overlap { source_id: String, at: u32 },
    #[error("Invalid block {id}: {reason}")]
    InvalidBlock { id: String, reason: &'static str },
    #[error("Exclusion reason does not match source evidence in {0}")]
    InvalidExclusion(String),
}

impl DocumentError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::TooLarge => "input_too_large",
            Self::Identity { .. } => "invalid_identity",
            Self::UnknownSource(_) => "unknown_source",
            Self::InvalidSpan { .. } => "invalid_source_span",
            Self::Missing { .. } => "unaccounted_source",
            Self::Overlap { .. } => "duplicate_source_ownership",
            Self::InvalidBlock { .. } => "invalid_block",
            Self::InvalidExclusion(_) => "invalid_exclusion",
        }
    }
}

/// Proves byte accounting, not that an interpretation is semantically correct.
pub(crate) fn validate(document: &Document) -> Result<BTreeMap<&str, &str>, DocumentError> {
    let mut sources = BTreeMap::new();
    let mut total = 0usize;
    for source in &document.sources {
        total = total
            .checked_add(source.text.len())
            .ok_or(DocumentError::TooLarge)?;
        if total > MAX_SOURCE_BYTES {
            return Err(DocumentError::TooLarge);
        }
        if source.id.is_empty()
            || sources
                .insert(source.id.as_str(), source.text.as_str())
                .is_some()
        {
            return Err(DocumentError::Identity {
                kind: "source",
                id: source.id.clone(),
            });
        }
    }
    let mut owned: BTreeMap<&str, Vec<(u32, u32)>> = BTreeMap::new();
    let mut claim = |span: &SourceSpan| -> Result<&str, DocumentError> {
        let (key, text) = sources
            .get_key_value(span.source.as_str())
            .ok_or_else(|| DocumentError::UnknownSource(span.source.clone()))?;
        let slice = text
            .get(span.start as usize..span.end as usize)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| DocumentError::InvalidSpan {
                source_id: span.source.clone(),
                start: span.start,
                end: span.end,
            })?;
        owned.entry(key).or_default().push((span.start, span.end));
        Ok(slice)
    };
    let mut ids = BTreeSet::new();
    for block in &document.blocks {
        if block.id.is_empty() || !ids.insert(&block.id) {
            return Err(DocumentError::Identity {
                kind: "block",
                id: block.id.clone(),
            });
        }
        let invalid = |reason| DocumentError::InvalidBlock {
            id: block.id.clone(),
            reason,
        };
        if block.fragments.is_empty() {
            return Err(invalid("no source fragments"));
        }
        if block.fragments[0].join_before != Join::None {
            return Err(invalid("first fragment cannot have a join"));
        }
        match &block.kind {
            BlockKind::Heading { level } if !(1..=6).contains(level) => {
                return Err(invalid("heading level must be 1 through 6"));
            }
            BlockKind::Code { language }
                if !language
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"_+.-".contains(&c)) =>
            {
                return Err(invalid("invalid code language"));
            }
            _ => {}
        }
        for fragment in &block.fragments {
            claim(&fragment.span)?;
        }
    }
    for excluded in &document.exclusions {
        let text = claim(&excluded.span)?;
        let valid = match excluded.reason {
            ExclusionReason::Whitespace => text.chars().all(char::is_whitespace),
            ExclusionReason::LineWrapHyphen => matches!(text, "-" | "\u{ad}"),
            ExclusionReason::PageFurniture | ExclusionReason::UserPolicy => true,
        };
        if !valid {
            return Err(DocumentError::InvalidExclusion(
                excluded.span.source.clone(),
            ));
        }
    }
    for (source_id, text) in &sources {
        let ranges = owned.entry(source_id).or_default();
        ranges.sort_unstable();
        let mut cursor = 0;
        for &(start, end) in ranges.iter() {
            if start < cursor {
                return Err(DocumentError::Overlap {
                    source_id: (*source_id).into(),
                    at: start,
                });
            }
            if start > cursor {
                return Err(DocumentError::Missing {
                    source_id: (*source_id).into(),
                    start: cursor,
                    end: start,
                });
            }
            cursor = end;
        }
        if cursor as usize != text.len() {
            return Err(DocumentError::Missing {
                source_id: (*source_id).into(),
                start: cursor,
                end: text.len() as u32,
            });
        }
    }
    Ok(sources)
}
