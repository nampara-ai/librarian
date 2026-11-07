//! Exact literal ownership alongside editable prose. No blanket number/Unicode normalization.
use super::contract::digest;
use pulldown_cmark::{Event, Parser, Tag};
use serde::{Deserialize, Serialize};
use std::{ops::Range, sync::OnceLock};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Artifact {
    pub artifact_id: String,
    pub marker: String,
    pub kind: String,
    pub sha256: String,
    pub start: usize,
    pub end: usize,
    pub content: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Protected {
    pub text: String,
    pub artifacts: Vec<Artifact>,
}

pub fn protect(source: &str) -> Protected {
    let mut spans: Vec<(Range<usize>, String)> = Vec::new();
    let trimmed = source.trim();
    if (trimmed.starts_with('{') || trimmed.starts_with('['))
        && serde_json::from_str::<serde_json::Value>(trimmed).is_ok()
    {
        spans.push((0..source.len(), "json".into()));
    }
    for (event, range) in Parser::new_ext(source, crate::markdown_options()).into_offset_iter() {
        let kind = match event {
            Event::Start(Tag::CodeBlock(_)) => Some("code_block"),
            Event::Start(Tag::Table(_)) => Some("table"),
            Event::Code(_) => Some("inline_code"),
            Event::InlineMath(_) | Event::DisplayMath(_) => Some("math"),
            Event::Html(_) | Event::InlineHtml(_) => Some("html"),
            Event::Start(Tag::Link { .. } | Tag::Image { .. }) => {
                // Preserve inline destinations while leaving link labels editable.
                let raw = &source[range.clone()];
                if let Some(at) = raw.rfind("](") {
                    let start = range.start + at + 2;
                    let end = range.end.saturating_sub(1);
                    if end > start {
                        spans.push((start..end, "link_destination".into()));
                    }
                }
                None
            }
            _ => None,
        };
        if let Some(kind) = kind {
            spans.push((range, kind.into()));
        }
    }
    // Frontmatter and link/reference definitions are literal metadata.
    let mut offset = 0;
    let mut frontmatter = None;
    for line in source.split_inclusive('\n') {
        let trimmed = line.trim();
        if offset == 0 && matches!(trimmed, "---" | "+++") {
            frontmatter = Some(trimmed);
        } else if let Some(delimiter) = frontmatter {
            if trimmed == delimiter {
                let body = &source[source.find('\n').map_or(0, |i| i + 1)..offset];
                if body
                    .lines()
                    .any(|s| s.contains(if delimiter == "+++" { '=' } else { ':' }))
                {
                    spans.push((0..offset + line.len(), "frontmatter".into()));
                }
                frontmatter = None;
            }
        } else if trimmed.starts_with('[') && trimmed.contains("]:") && !trimmed.starts_with("[^") {
            spans.push((offset..offset + line.len(), "reference_definition".into()));
        }
        offset += line.len();
    }
    for (open, close) in [("\\(", "\\)"), ("\\[", "\\]")] {
        let mut from = 0;
        while let Some(at) = source[from..].find(open) {
            let start = from + at;
            if let Some(end) = source[start + 2..].find(close) {
                let end = start + 2 + end + 2;
                spans.push((start..end, "math".into()));
                from = end;
            } else {
                break;
            }
        }
    }
    // A damaged math delimiter (for example whitespace immediately before `$`)
    // may be rejected by Markdown's parser. Recognizable TeX remains literal.
    static DAMAGED_MATH: OnceLock<regex::Regex> = OnceLock::new();
    for m in DAMAGED_MATH
        .get_or_init(|| regex::Regex::new(r"\$[^\n$]*[\\_^=][^\n$]*\$").unwrap())
        .find_iter(source)
    {
        if !source[..m.start()].ends_with('\\')
            && !spans.iter().any(|(r, _)| r.contains(&m.start()))
        {
            spans.push((m.range(), "math".into()));
        }
    }
    static LITERALS: OnceLock<regex::Regex> = OnceLock::new();
    let literals = LITERALS.get_or_init(|| {
        regex::Regex::new(
            r#"(?x)
        (?:https?://|ftp://|mailto:)[^\s<>\[\]`()"']+ |
        (?:[A-Za-z]:\\|~/|\.\.?/)[^\s<>`]+ |
        /[\pL\pN_.-]+(?:/[\pL\pN_.-]+)+ |
        \$\{[A-Za-z_][A-Za-z_0-9]*\}|\$[A-Za-z_][A-Za-z_0-9]* |
        --[A-Za-z][A-Za-z0-9_-]*(?:=[^\s`]+)? |
        \b[\pL][\pL\pN]*(?:_+[\pL\pN]+)+\b |
        \b[A-Za-z_][A-Za-z_0-9]*(?:::[A-Za-z_][A-Za-z_0-9]*)+\b |
        \b[0-9a-fA-F]{16,}\b |
        \bv?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\b |
        \b0[0-9]+\b |
        \[\^[^\]\r\n]+\] |
        ⟪LIBRARIAN_ARTIFACT_[^⟫]*⟫
        "#,
        )
        .expect("Literal tokenizer is a static pattern")
    });
    for m in literals.find_iter(source) {
        if m.as_str().bytes().all(|c| c.is_ascii_digit()) && source[..m.start()].ends_with('.') {
            continue;
        }
        spans.push((m.range(), "literal".into()));
    }
    spans.sort_by_key(|(r, _)| (r.start, std::cmp::Reverse(r.end)));
    // Merge overlaps instead of discarding a span's unprotected suffix.
    let mut merged: Vec<(Range<usize>, String)> = Vec::new();
    for (span, kind) in spans {
        if let Some((last, _)) = merged.last_mut()
            && span.start < last.end
        {
            last.end = last.end.max(span.end);
        } else {
            merged.push((span, kind));
        }
    }
    let mut artifacts = Vec::new();
    let mut text = String::new();
    let mut position = 0;
    for (span, kind) in merged {
        let content = &source[span.clone()];
        let hash = digest(content);
        let artifact_id = format!("artifact:{}:{}:{}", span.start, span.end, hash);
        let mut serial = artifacts.len();
        let marker = loop {
            let id = format!("⟪LIBRARIAN_ARTIFACT_{}_{}⟫", &hash[..16], serial);
            if !source.contains(&id) {
                break id;
            }
            serial += 1;
        };
        text.push_str(&source[position..span.start]);
        text.push_str(&marker);
        artifacts.push(Artifact {
            artifact_id,
            marker,
            kind,
            sha256: hash,
            start: span.start,
            end: span.end,
            content: content.into(),
        });
        position = span.end;
    }
    text.push_str(&source[position..]);
    Protected { text, artifacts }
}

pub fn restore(text: &str, artifacts: &[Artifact]) -> Result<String, String> {
    // Validate the transport BEFORE inserting any source bytes. A literal source
    // can itself contain marker-looking strings; those are never interpreted.
    let mut remaining = text.to_owned();
    for artifact in artifacts {
        if digest(&artifact.content) != artifact.sha256 {
            return Err("Protected artifact hash mismatch".into());
        }
        if text.matches(&artifact.marker).count() != 1 {
            return Err(format!(
                "Protected artifact {} must occur exactly once",
                artifact.artifact_id
            ));
        }
        remaining = remaining.replacen(&artifact.marker, "", 1);
    }
    if remaining.contains("⟪LIBRARIAN_ARTIFACT_") {
        return Err("Unknown transport artifact in edited output".into());
    }
    let mut replacements: Vec<_> = artifacts
        .iter()
        .map(|a| (text.find(&a.marker).unwrap(), a))
        .collect();
    replacements.sort_by_key(|(at, _)| std::cmp::Reverse(*at));
    let mut output = text.to_owned();
    for (at, artifact) in replacements {
        output.replace_range(at..at + artifact.marker.len(), &artifact.content);
    }
    Ok(output)
}

/// Carry an unfinished block without cutting an inline literal or a table.
pub fn handoff(text: &str) -> super::Handoff {
    let mut handoff = super::handoff(text);
    let mut boundary = handoff.emitted.len();
    for artifact in protect(text).artifacts {
        if artifact.start < boundary && boundary < artifact.end {
            boundary = artifact.start;
            break;
        }
    }
    handoff.emitted = text[..boundary].into();
    handoff.pending = text[boundary..].into();
    handoff.context = super::tail(&handoff.emitted, 1200);
    handoff
}

pub fn literals_preserved(before: &str, after: &str) -> bool {
    let mut after = after.to_owned();
    for artifact in protect(before).artifacts {
        if let Some(at) = after.find(&artifact.content) {
            after.replace_range(at..at + artifact.content.len(), "");
        } else {
            return false;
        }
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    // Byte ownership must survive every small split offset, Unicode boundary,
    // parser span and literal-marker collision, not just a convenient chunk size.
    #[test]
    fn mixed_literals_survive_chunking_and_cannot_be_silently_mutated() {
        let source = concat!(
            "---\ntitle: 'Müller: 日本語'\n---\n\n",
            "rough prose e\u{301} العربية 🦀 with `printf '%s\\n' \"$HOME\"` and $x_{i}^{2}$.\n\n",
            "[a link](https://example.org/a?q=0012#part) uses --dry-run and model_name.\n\n",
            "```json\n{\"value\": -0.00120, \"id\": \"0012\"}\n```\n\n",
            "| Name | Value |\n| --- | --- |\n| α | −3.00 |\n\n",
            "\\[ a_{n+1} = \\frac{x}{2} \\]\n\n",
            "```csv\nname,value\n\"comma, quote\",1.20\n```\n\n",
            "Literal transport example: ⟪LIBRARIAN_ARTIFACT_abc_0⟫.\n\n",
            "    significant indentation\n    and spacing  \n"
        );
        let protected = protect(source);
        let edited = protected.text.replace("rough prose", "Polished prose");
        assert_eq!(
            restore(&edited, &protected.artifacts).unwrap(),
            source.replace("rough prose", "Polished prose")
        );
        for limit in 4..256 {
            let mut restored = String::new();
            for (a, b) in super::super::ranges(&protected.text, limit) {
                let part = &protected.text[a..b];
                let artifacts: Vec<_> = protected
                    .artifacts
                    .iter()
                    .filter(|x| part.contains(&x.marker))
                    .cloned()
                    .collect();
                restored.push_str(&restore(part, &artifacts).unwrap());
            }
            assert_eq!(restored, source, "lost ownership at byte limit {limit}");
        }
        let marker = &protected.artifacts[0].marker;
        assert!(restore(&protected.text.replace(marker, ""), &protected.artifacts).is_err());
        assert!(restore(&format!("{}{marker}", protected.text), &protected.artifacts).is_err());
        assert!(
            restore(
                &format!("{} ⟪LIBRARIAN_ARTIFACT_invented⟫", protected.text),
                &protected.artifacts
            )
            .is_err()
        );
        let mut corrupt = protected.artifacts.clone();
        corrupt[0].content.push('x');
        assert!(restore(&protected.text, &corrupt).is_err());
        assert!(!literals_preserved(
            source,
            &source.replace("-0.00120", "-0.0012")
        ));
        let huge = format!(
            "Introduction.\n\n${}$ trailing explanation",
            "x_{i} + ".repeat(1400)
        );
        let carried = handoff(&huge);
        assert_eq!(format!("{}{}", carried.emitted, carried.pending), huge);
        assert_eq!(carried.emitted, "Introduction.\n\n");
    }
}
