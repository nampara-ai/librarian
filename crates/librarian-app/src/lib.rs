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
    #[error("{0}")]
    Corpus(String),
}

impl AppError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::Document(error) => error.code(),
            Self::EmptyInput => "empty_input",
            Self::Corpus(_) => "invalid_corpus",
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
    RenderCorpus {
        segments: Vec<librarian_core::corpus::Segment>,
    },
    PlanText {
        content: String,
        chunk_bytes: usize,
    },
    EditorialPlan {
        content: String,
        chunk_bytes: usize,
    },
    EditorialHandoff {
        content: String,
    },
    EditorialContract,
    ProtectEditorial {
        content: String,
    },
    RestoreEditorial {
        content: String,
        artifacts: Vec<librarian_core::editorial::protection::Artifact>,
    },
    ReviseEditorial {
        content: String,
        revision: librarian_core::editorial::contract::Revision,
    },
    InspectEdit {
        source: String,
        candidate: String,
    },
    Filename {
        template: String,
        metadata: librarian_core::corpus::Metadata,
        id: String,
        accession: u64,
    },
}

#[derive(Debug, Serialize)]
#[serde(tag = "status", rename_all = "snake_case")]
enum Response {
    Ok {
        schema_version: u32,
        result: serde_json::Value,
    },
    Error {
        schema_version: u32,
        code: String,
        message: String,
    },
}

/// Versioned boundary shared by WASM and native hosts.
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
                    Action::Convert { format, content } => convert(format, content).map(|v|serde_json::json!(v)),
                    Action::Assemble { document } => render(&document).map(|v|serde_json::json!(v)).map_err(AppError::from),
                    Action::RenderCorpus { segments } => librarian_core::corpus::render_segments(&segments).map(|markdown|serde_json::json!({"stats":librarian_core::inspect_markdown(&markdown),"markdown":markdown,"segments":segments})).map_err(AppError::Corpus),
                    Action::PlanText {content,chunk_bytes} => {
                        if !(4..=32000).contains(&chunk_bytes) {Err(AppError::Corpus("chunk_bytes must be 4..32000".into()))}
                        else {Ok(serde_json::json!({"ranges":librarian_core::corpus::split_text(&content,chunk_bytes)}))}
                    }
                    Action::InspectEdit {source,candidate} => Ok(serde_json::json!({"findings":librarian_core::corpus::edit_findings(&source,&candidate)})),
                    Action::EditorialPlan {content,chunk_bytes} => {
                        if !(4..=32000).contains(&chunk_bytes){Err(AppError::Corpus("chunk_bytes must be 4..32000".into()))}
                        else{Ok(serde_json::json!({"ranges":librarian_core::editorial::ranges(&content,chunk_bytes)}))}
                    },
                    Action::EditorialHandoff {content} => Ok(serde_json::json!(librarian_core::editorial::handoff(&content))),
                    Action::EditorialContract => Ok(serde_json::json!({"version":librarian_core::editorial::VERSION,"copyeditor":librarian_core::editorial::COPYEDITOR,"copyeditor_version":librarian_core::editorial::COPYEDITOR_VERSION,"reviewer":librarian_core::editorial::REVIEWER,"reviewer_version":librarian_core::editorial::REVIEWER_VERSION,"protocol":librarian_core::editorial::contract::PROTOCOL,"response_schema":librarian_core::editorial::contract::schema(),"visual_reader":librarian_core::editorial::VISION,"typesetter":librarian_core::editorial::TYPESETTER_V2})),
                    Action::ProtectEditorial {content}=>Ok(serde_json::json!(librarian_core::editorial::protection::protect(&content))),
                    Action::RestoreEditorial {content,artifacts}=>librarian_core::editorial::protection::restore(&content,&artifacts).map(|text|serde_json::json!({"markdown":text})).map_err(AppError::Corpus),
                    Action::ReviseEditorial {content,revision}=>librarian_core::editorial::contract::revised(&content,&revision).map(|text|serde_json::json!({"markdown":text})).map_err(AppError::Corpus),
                    Action::Filename {template,metadata,id,accession} => librarian_core::corpus::safe_filename(&template,&metadata,&id,accession).map(|name|serde_json::json!({"filename":name})).map_err(AppError::Corpus),
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
