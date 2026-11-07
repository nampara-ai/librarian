use librarian_core::*;
use pulldown_cmark::{Event, Parser, Tag};

fn span(source: &str, start: u32, end: u32) -> SourceSpan {
    SourceSpan {
        source: source.into(),
        start,
        end,
    }
}

fn one(text: &str, kind: BlockKind) -> Document {
    Document {
        sources: vec![SourceText {
            id: "p1".into(),
            text: text.into(),
        }],
        blocks: vec![Block {
            id: "b1".into(),
            kind,
            fragments: vec![Fragment {
                span: span("p1", 0, text.len() as u32),
                join_before: Join::None,
            }],
        }],
        exclusions: vec![],
    }
}

#[test]
fn one_paragraph_can_cross_pages_without_importing_a_footer() {
    let mut doc = one("The results indicate that the", BlockKind::Paragraph);
    doc.sources.push(SourceText {
        id: "footer".into(),
        text: "12".into(),
    });
    doc.sources.push(SourceText {
        id: "p2".into(),
        text: "intervention worked.".into(),
    });
    doc.blocks[0].fragments.push(Fragment {
        span: span("p2", 0, 20),
        join_before: Join::Space,
    });
    doc.exclusions.push(ExcludedSpan {
        span: span("footer", 0, 2),
        reason: ExclusionReason::PageFurniture,
    });
    let rendered = render(&doc).unwrap();
    assert_eq!(
        rendered.markdown,
        "The results indicate that the intervention worked.\n"
    );
    assert_eq!(rendered.blocks[0].sources.len(), 2);
    assert_eq!(rendered.stats.paragraphs, 1);
}

#[test]
fn ownership_rejects_loss_and_overlap_but_keeps_intentional_repetition() {
    let mut doc = one("Repeat.", BlockKind::Paragraph);
    doc.sources.push(SourceText {
        id: "p2".into(),
        text: "Repeat.".into(),
    });
    assert!(matches!(render(&doc), Err(DocumentError::Missing { .. })));
    let mut second = doc.blocks[0].clone();
    second.id = "b2".into();
    second.fragments[0].span.source = "p2".into();
    doc.blocks.push(second);
    let rendered = render(&doc).unwrap();
    assert_eq!(rendered.markdown, "Repeat.\n\nRepeat.\n");
    assert_eq!(
        rendered.source_digests[0].blake3,
        rendered.source_digests[1].blake3
    );
    assert_ne!(
        rendered.source_digests[0].source,
        rendered.source_digests[1].source
    );
    doc.blocks[1].fragments[0].span.source = "p1".into();
    assert!(matches!(render(&doc), Err(DocumentError::Overlap { .. })));
}

#[test]
fn unicode_splits_accept_only_utf8_boundaries() {
    let text = "A😀 café e\u{301} 中文";
    for cut in 1..text.len() {
        let mut doc = one(text, BlockKind::Markdown);
        doc.blocks[0].fragments = vec![
            Fragment {
                span: span("p1", 0, cut as u32),
                join_before: Join::None,
            },
            Fragment {
                span: span("p1", cut as u32, text.len() as u32),
                join_before: Join::None,
            },
        ];
        if text.is_char_boundary(cut) {
            assert_eq!(render(&doc).unwrap().markdown, format!("{text}\n"));
        } else {
            assert!(matches!(
                render(&doc),
                Err(DocumentError::InvalidSpan { .. })
            ));
        }
    }
}

#[test]
fn missing_and_reversed_ranges_are_explicit_errors() {
    let mut doc = one("abc", BlockKind::Paragraph);
    doc.blocks[0].fragments[0].span.end = 2;
    assert!(matches!(
        render(&doc),
        Err(DocumentError::Missing {
            start: 2,
            end: 3,
            ..
        })
    ));
    doc.blocks[0].fragments[0].span.start = 3;
    assert!(matches!(
        render(&doc),
        Err(DocumentError::InvalidSpan { .. })
    ));
    doc.blocks[0].fragments[0].span.source = "unknown".into();
    assert!(matches!(render(&doc), Err(DocumentError::UnknownSource(_))));
}

#[test]
fn meaningful_text_cannot_be_excluded_as_whitespace() {
    let mut doc = one("important", BlockKind::Paragraph);
    doc.exclusions.push(ExcludedSpan {
        span: doc.blocks[0].fragments[0].span.clone(),
        reason: ExclusionReason::Whitespace,
    });
    doc.blocks.clear();
    assert!(matches!(
        render(&doc),
        Err(DocumentError::InvalidExclusion(_))
    ));
}

#[test]
fn literal_prose_does_not_turn_into_markup() {
    for text in [
        "# not a heading",
        "1. not a list",
        "1) not a list",
        "1.",
        "1)",
        "- not a list",
        "---",
        "    not code",
        "a *literal* [link](url) & $5",
        "<script>x</script>",
        "literal | cells",
        "price is $5.00 (USD).",
        "~~not strikethrough~~",
        "~~~",
    ] {
        let rendered = render(&one(text, BlockKind::Paragraph)).unwrap();
        let mut recovered = String::new();
        for event in Parser::new_ext(&rendered.markdown, markdown_options()) {
            match event {
                Event::Start(Tag::Paragraph) | Event::End(_) => {}
                Event::Text(value) => recovered.push_str(&value),
                other => panic!("unexpected structure in {text:?}: {other:?}"),
            }
        }
        assert_eq!(recovered, text);
    }
    assert_eq!(
        render(&one("A normal sentence.", BlockKind::Paragraph))
            .unwrap()
            .markdown,
        "A normal sentence.\n"
    );
}

#[test]
fn code_fences_cannot_be_closed_by_the_payload() {
    let code = "const example = `text`;\n```\n# still code\n";
    let rendered = render(&one(
        code,
        BlockKind::Code {
            language: "js".into(),
        },
    ))
    .unwrap();
    assert!(rendered.markdown.starts_with("````js\n"));
    let recovered: String = Parser::new_ext(&rendered.markdown, markdown_options())
        .filter_map(|e| match e {
            Event::Text(text) => Some(text.into_string()),
            _ => None,
        })
        .collect();
    assert_eq!(recovered, code);
    assert_eq!(rendered.stats.code_blocks, 1);
    assert_eq!(rendered.stats.headings, 0);
}

#[test]
fn source_mermaid_is_protected_without_claiming_validation() {
    let source = "flowchart LR\n  A[Source] --> B[Result]";
    let rendered = render(&one(
        source,
        BlockKind::Code {
            language: "mermaid".into(),
        },
    ))
    .unwrap();
    assert_eq!(rendered.markdown, format!("```mermaid\n{source}\n```\n"));
    assert_eq!(rendered.stats.mermaid_blocks, 1);
}

#[test]
fn generated_headings_and_math_have_the_intended_structure() {
    let heading = render(&one("A *literal* title", BlockKind::Heading { level: 2 })).unwrap();
    assert_eq!(heading.markdown, "## A \\*literal\\* title\n");
    assert_eq!(heading.stats.headings, 1);
    let heading = render(&one("Keep these ###", BlockKind::Heading { level: 2 })).unwrap();
    let recovered: String = Parser::new_ext(&heading.markdown, markdown_options())
        .filter_map(|event| match event {
            Event::Text(text) => Some(text.into_string()),
            _ => None,
        })
        .collect();
    assert_eq!(recovered, "Keep these ###");
    let equation = render(&one("x^{2} + y^{2} = z^{2}", BlockKind::DisplayMath)).unwrap();
    assert_eq!(equation.stats.equations, 1);
    assert!(render(&one("a\nb", BlockKind::Heading { level: 1 })).is_err());
    assert!(render(&one("x $$ y", BlockKind::DisplayMath)).is_err());
}
