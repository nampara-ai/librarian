use librarian_app::{AppError, InputFormat, convert, execute_json, ingest};
use librarian_core::{MAX_SOURCE_BYTES, render};

#[test]
fn markdown_keeps_tables_footnotes_math_and_code_verbatim() {
    let source = "# Report\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\nClaim.[^a]\n\n[^a]: Source.\n\n$$\nx^2\n$$\n\n```mermaid\nflowchart LR\nA --> B\n```\n";
    let result = convert(InputFormat::Markdown, source.into()).unwrap();
    assert_eq!(result.markdown, source);
    assert_eq!(result.stats.tables, 1);
    assert_eq!(result.stats.footnotes, 1);
    assert_eq!(result.stats.equations, 1);
    assert_eq!(result.stats.mermaid_blocks, 1);
}

#[test]
fn text_import_accounts_for_blank_lines_and_keeps_repetition() {
    let doc = ingest(InputFormat::Text, "\nRepeat.\r\n\r\nRepeat.\n\n".into()).unwrap();
    assert_eq!(doc.exclusions.len(), 3);
    assert_eq!(render(&doc).unwrap().markdown, "Repeat.\n\nRepeat.\n");
}

#[test]
fn empty_or_oversized_input_is_not_successful_extraction() {
    assert!(matches!(
        convert(InputFormat::Text, " \n\t".into()),
        Err(AppError::EmptyInput)
    ));
    let response = convert(InputFormat::Text, "x".repeat(MAX_SOURCE_BYTES + 1)).unwrap_err();
    assert_eq!(response.code(), "input_too_large");
}

#[test]
fn wire_contract_rejects_wrong_versions_and_unknown_options() {
    for (request, expected) in [
        (
            r#"{"schema_version":2,"action":{"type":"convert","format":"text","content":"hello"}}"#,
            "unsupported_schema",
        ),
        (
            r#"{"schema_version":1,"action":{"type":"convert","format":"text","content":"hello","invent":true}}"#,
            "invalid_request",
        ),
        ("not json", "invalid_request"),
    ] {
        let response: serde_json::Value = serde_json::from_str(&execute_json(request)).unwrap();
        assert_eq!(response["status"], "error");
        assert_eq!(response["code"], expected);
    }
}
