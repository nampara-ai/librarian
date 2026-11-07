//! Host-independent application entry points. Effects belong to host adapters.
#![forbid(unsafe_code)]

use librarian_core::{
    Block, BlockKind, Document, DocumentError, ExcludedSpan, ExclusionReason, Fragment, Join,
    MAX_SOURCE_BYTES, RenderedDocument, SourceSpan, SourceText, render,
};
use serde::{Deserialize, Serialize};
use thiserror::Error;

pub const SCHEMA_VERSION: u32 = 1;
const MAX_REQUEST_BYTES: usize = MAX_SOURCE_BYTES * 8;

#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum InputFormat {
    Text,
    Markdown,
}

#[derive(Debug, Error)]
pub enum AppError {
    #[error(transparent)]
    Document(#[from] DocumentError),
    #[error("Input contains no document content")]
    EmptyInput,
}

impl AppError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::Document(error) => error.code(),
            Self::EmptyInput => "empty_input",
        }
    }
}

/// UTF-8 input only. This slice does not perform OCR, copyediting, or classification.
pub fn ingest(format: InputFormat, content: String) -> Result<Document, AppError> {
    if content.len() > MAX_SOURCE_BYTES {
        return Err(DocumentError::TooLarge.into());
    }
    if content.trim().is_empty() {
        return Err(AppError::EmptyInput);
    }
    // Occurrence IDs are scoped to this batch. Content digests are separate output
    // evidence; identical content must never collapse distinct occurrences.
    let id = "input-1".to_string();
    let span = |start: usize, end: usize| SourceSpan {
        source: id.clone(),
        start: start as u32,
        end: end as u32,
    };
    let mut blocks = Vec::new();
    let mut exclusions = Vec::new();
    let mut append = |start, end, kind| {
        blocks.push(Block {
            id: format!("block-{}", blocks.len() + 1),
            kind,
            fragments: vec![Fragment {
                span: span(start, end),
                join_before: Join::None,
            }],
        })
    };
    match format {
        InputFormat::Markdown => append(0, content.len(), BlockKind::Markdown),
        InputFormat::Text => {
            let mut offset = 0;
            let mut paragraph = None;
            for line in content.split_inclusive('\n') {
                let end = offset + line.len();
                if line.trim().is_empty() {
                    if let Some(start) = paragraph.take() {
                        append(start, offset, BlockKind::Paragraph);
                    }
                    exclusions.push(ExcludedSpan {
                        span: span(offset, end),
                        reason: ExclusionReason::Whitespace,
                    });
                } else if paragraph.is_none() {
                    paragraph = Some(offset);
                }
                offset = end;
            }
            if let Some(start) = paragraph {
                append(start, content.len(), BlockKind::Paragraph);
            }
        }
    }
    Ok(Document {
        sources: vec![SourceText { id, text: content }],
        blocks,
        exclusions,
    })
}

pub fn convert(format: InputFormat, content: String) -> Result<RenderedDocument, AppError> {
    Ok(render(&ingest(format, content)?)?)
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    schema_version: u32,
    action: Action,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
enum Action {
    Convert {
        format: InputFormat,
        content: String,
    },
    Assemble {
        document: Document,
    },
}

#[derive(Debug, Serialize)]
#[serde(tag = "status", rename_all = "snake_case")]
enum Response {
    Ok {
        schema_version: u32,
        result: RenderedDocument,
    },
    Error {
        schema_version: u32,
        code: String,
        message: String,
    },
}

/// Versioned boundary shared by WASM and the native conformance runner.
/// Invalid requests are explicit outcomes, not empty successful documents.
pub fn execute_json(input: &str) -> String {
    let error = |code: &str, message: String| Response::Error {
        schema_version: SCHEMA_VERSION,
        code: code.into(),
        message,
    };
    let response = if input.len() > MAX_REQUEST_BYTES {
        error(
            "input_too_large",
            "JSON request exceeds the batch limit".into(),
        )
    } else {
        match serde_json::from_str::<Request>(input) {
            Err(e) => error("invalid_request", e.to_string()),
            Ok(request) if request.schema_version != SCHEMA_VERSION => error(
                "unsupported_schema",
                format!(
                    "Expected schema {SCHEMA_VERSION}, got {}",
                    request.schema_version
                ),
            ),
            Ok(request) => {
                let result = match request.action {
                    Action::Convert { format, content } => convert(format, content),
                    Action::Assemble { document } => render(&document).map_err(AppError::from),
                };
                match result {
                    Ok(result) => Response::Ok {
                        schema_version: SCHEMA_VERSION,
                        result,
                    },
                    Err(e) => error(e.code(), e.to_string()),
                }
            }
        }
    };
    // Response contains only owned strings, integers and structs, all JSON-representable.
    serde_json::to_string(&response).expect("response is JSON-representable")
}
