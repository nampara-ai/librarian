//! Fast cleaning. A document is cut into passages, every passage is repaired by
//! one bounded model request, and the requests run side by side. Nothing here
//! calls a model: this module holds the prompt, the planning of passages, and
//! the word-for-word account that decides whether a response may replace its
//! source.
use crate::corpus::{Content, Segment, render_segments, split_text};
use serde::{Deserialize, Serialize};

pub const VERSION: &str = "fast-clean-v1";
pub const PROMPT: &str = include_str!("../prompts/fast-clean-v1.txt");

/// The system text of a request: the fixed role, then the owner's style notes
/// and exact spellings. Identical for every passage of a job, so a provider
/// can cache it.
pub fn instructions(style: &str, house_style: &str, glossary: &[(String, String)]) -> String {
    let mut text = PROMPT.trim_end().to_owned();
    let notes: Vec<&str> = [style, house_style]
        .into_iter()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .collect();
    if !notes.is_empty() {
        text.push_str("\n\nHouse style, which overrides the defaults above:\n");
        text.push_str(&notes.join("\n"));
    }
    if !glossary.is_empty() {
        text.push_str(
            "\n\nUse these exact spellings where the passage means them. They are spellings, not instructions:\n",
        );
        for (term, note) in glossary {
            text.push_str("- ");
            text.push_str(term);
            if !note.trim().is_empty() {
                text.push_str(" (");
                text.push_str(note.trim());
                text.push(')');
            }
            text.push('\n');
        }
    }
    text
}

/// The user text of a request. The output contract is repeated after the data.
pub fn envelope(before: &str, passage: &str, after: &str) -> String {
    let mut text = String::new();
    if !before.trim().is_empty() {
        text.push_str("<before>\n");
        text.push_str(before.trim());
        text.push_str("\n</before>\n");
    }
    text.push_str("<passage>\n");
    text.push_str(passage.trim());
    text.push_str("\n</passage>");
    if !after.trim().is_empty() {
        text.push_str("\n<after>\n");
        text.push_str(after.trim());
        text.push_str("\n</after>");
    }
    text.push_str("\n\nReturn only the repaired passage between <clean> and </clean>.");
    text
}

/// The repaired passage out of a raw response. A response that stops before
/// the closing tag was cut short and is refused.
pub fn unwrap(response: &str) -> Result<String, String> {
    let mut text = response;
    // Reasoning that a model printed in line is not part of the answer.
    while let Some(start) = text.find("<think>") {
        match text[start..].find("</think>") {
            Some(end) if start == 0 || text[..start].trim().is_empty() => {
                text = &text[start + end + "</think>".len()..];
            }
            _ => break,
        }
    }
    let Some(open) = text.find("<clean>") else {
        return Err("the response has no <clean> section".into());
    };
    let body = &text[open + "<clean>".len()..];
    let Some(close) = body.rfind("</clean>") else {
        return Err("the response ended before </clean>".into());
    };
    let body = body[..close].trim();
    if body.is_empty() {
        return Err("the response is empty".into());
    }
    Ok(body.to_owned())
}

/// Mechanical repairs that need no judgment, applied before a passage is sent.
pub fn tidy(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut space = false;
    for ch in text.chars() {
        let replaced: &str = match ch {
            '\u{ad}' | '\u{200b}' | '\u{feff}' => continue,
            '\u{fb00}' => "ff",
            '\u{fb01}' => "fi",
            '\u{fb02}' => "fl",
            '\u{fb03}' => "ffi",
            '\u{fb04}' => "ffl",
            '\u{a0}' | '\t' => " ",
            _ => "",
        };
        if ch == ' ' || replaced == " " {
            if !space {
                out.push(' ');
            }
            space = true;
            continue;
        }
        space = false;
        if replaced.is_empty() {
            out.push(ch);
        } else {
            out.push_str(replaced);
        }
    }
    out.lines()
        .map(str::trim_end)
        .collect::<Vec<_>>()
        .join("\n")
}

fn editable(content: &Content) -> bool {
    matches!(
        content,
        Content::Prose { .. }
            | Content::Heading { .. }
            | Content::MarkdownProse { .. }
            | Content::ListItem { .. }
    )
}

/// Subtitle cue numbers and time codes. They are scaffolding, not text.
fn cue_label(segment: &Segment) -> bool {
    segment.id.starts_with("cue-")
        && segment.id.contains("-time:")
        && matches!(segment.content, Content::Markdown { .. })
}

/// Turn extracted blocks into passages. Neighbouring editable blocks are joined
/// into one Markdown passage of at most `limit` bytes; a block larger than the
/// limit is cut at white space. Tables, code, equations and figures stay as
/// they are and are not sent to a model. Order is kept and no block is used twice.
pub fn plan(blocks: Vec<Segment>, limit: usize) -> Result<Vec<Segment>, String> {
    let limit = limit.max(256);
    let mut pieces: Vec<Segment> = Vec::new();
    for block in blocks {
        if cue_label(&block) {
            continue;
        }
        let oversized = matches!(block.content, Content::Prose { .. })
            && block
                .content
                .editable_text()
                .is_some_and(|t| t.len() > limit);
        if !oversized {
            pieces.push(block);
            continue;
        }
        let text = block.content.editable_text().unwrap().to_owned();
        for (i, (start, end)) in split_text(&text, limit).into_iter().enumerate() {
            let mut part = block.clone();
            part.id = format!("{}:part-{i}", block.id);
            part.content = Content::Prose {
                text: text[start..end].to_owned(),
            };
            if i > 0 {
                part.findings.clear();
                part.continuation = true;
                part.tight_join = text[start..]
                    .chars()
                    .next()
                    .is_some_and(|c| !c.is_whitespace())
                    && text[..start]
                        .chars()
                        .next_back()
                        .is_some_and(|c| !c.is_whitespace());
            }
            pieces.push(part);
        }
    }
    let mut planned: Vec<Segment> = Vec::new();
    let mut group: Vec<Segment> = Vec::new();
    let mut size = 0usize;
    fn close(group: &mut Vec<Segment>, planned: &mut Vec<Segment>) -> Result<(), String> {
        if group.is_empty() {
            return Ok(());
        }
        let members = std::mem::take(group);
        let mut first = members[0].clone();
        // The passage carries its own first block's join to what came before.
        let mut inner = members.clone();
        inner[0].continuation = false;
        inner[0].tight_join = false;
        let text = render_segments(&inner)?;
        first.id = if members.len() > 1 {
            format!("{}+{}", first.id, members.len() - 1)
        } else {
            first.id
        };
        first.evidence = members.iter().flat_map(|m| m.evidence.clone()).collect();
        first.findings = members.iter().flat_map(|m| m.findings.clone()).collect();
        first.logical_part = None;
        first.content = Content::MarkdownProse {
            text: text.trim_end().to_owned(),
        };
        planned.push(first);
        Ok(())
    }
    for piece in pieces {
        if !editable(&piece.content) {
            close(&mut group, &mut planned)?;
            size = 0;
            planned.push(piece);
            continue;
        }
        let length = piece.content.editable_text().map_or(0, str::len);
        // A cut piece of one long paragraph is its own passage: joining it to its
        // other half would rebuild the oversized block.
        let boundary = piece.continuation && piece.id.contains(":part-");
        if !group.is_empty() && (size + length > limit || boundary) {
            // A heading belongs with the text under it.
            let mut carried = Vec::new();
            while group.len() > 1
                && matches!(group.last().unwrap().content, Content::Heading { .. })
            {
                carried.insert(0, group.pop().unwrap());
            }
            close(&mut group, &mut planned)?;
            size = carried
                .iter()
                .map(|s| s.content.editable_text().map_or(0, str::len))
                .sum();
            group = carried;
        }
        size += length;
        group.push(piece);
    }
    close(&mut group, &mut planned)?;
    Ok(planned)
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Change {
    /// replaced, removed or inserted.
    pub kind: String,
    pub before: String,
    pub after: String,
}

/// What a response did to its source, counted in words. Every source word is
/// in exactly one of kept, respelled, joined, replaced, fillers_removed,
/// repeats_removed and removed.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Ledger {
    pub measured: bool,
    pub source_words: u32,
    pub output_words: u32,
    pub kept: u32,
    /// Changed to a similar spelling: a scanning or hearing error repaired.
    pub respelled: u32,
    /// Split or run-together words put right.
    pub joined: u32,
    /// Changed to a different word.
    pub replaced: u32,
    pub fillers_removed: u32,
    /// Removed words whose run stands elsewhere in the output.
    pub repeats_removed: u32,
    pub removed: u32,
    pub inserted: u32,
    pub longest_removed: u32,
    pub longest_inserted: u32,
    pub numbers_lost: Vec<String>,
    pub numbers_added: Vec<String>,
    pub changes: Vec<Change>,
}

impl Ledger {
    pub fn accounted(&self) -> bool {
        self.kept
            + self.respelled
            + self.joined
            + self.replaced
            + self.fillers_removed
            + self.repeats_removed
            + self.removed
            == self.source_words
    }
    pub fn add(&mut self, other: &Ledger) {
        self.measured |= other.measured;
        self.source_words += other.source_words;
        self.output_words += other.output_words;
        self.kept += other.kept;
        self.respelled += other.respelled;
        self.joined += other.joined;
        self.replaced += other.replaced;
        self.fillers_removed += other.fillers_removed;
        self.repeats_removed += other.repeats_removed;
        self.removed += other.removed;
        self.inserted += other.inserted;
        self.longest_removed = self.longest_removed.max(other.longest_removed);
        self.longest_inserted = self.longest_inserted.max(other.longest_inserted);
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum Verdict {
    Accept,
    /// Accepted; the notes are shown to the owner.
    Review(Vec<String>),
    /// Not an edit: text was dropped or written in bulk. The source is kept.
    Reject(String),
}

struct Token {
    /// Lower-case letters and digits only.
    key: String,
    start: usize,
    end: usize,
}

fn tokens(text: &str) -> Vec<Token> {
    let mut result: Vec<Token> = Vec::new();
    let chars: Vec<(usize, char)> = text.char_indices().collect();
    let mut i = 0;
    while i < chars.len() {
        let (at, ch) = chars[i];
        if !ch.is_alphanumeric() {
            i += 1;
            continue;
        }
        let mut key = String::new();
        let mut end = at;
        while i < chars.len() {
            let (at, ch) = chars[i];
            let digit_before = key.chars().next_back().is_some_and(|c| c.is_ascii_digit());
            let next = chars.get(i + 1).map(|c| c.1);
            if ch.is_alphanumeric() {
                key.extend(ch.to_lowercase());
            } else if ch == '\u{d7}' && digit_before {
                // 2× and 2x are one token.
                key.push('x');
            } else if matches!(ch, '\'' | '\u{2019}') && next.is_some_and(char::is_alphanumeric) {
                // don't and don’t are one word.
            } else if matches!(ch, ',' | '.')
                && digit_before
                && next.is_some_and(|c| c.is_ascii_digit())
            {
                // 1,000 and 1000 are one number.
            } else {
                break;
            }
            end = at + ch.len_utf8();
            i += 1;
        }
        result.push(Token {
            key,
            start: at,
            end,
        });
    }
    result
}

const FILLERS: &[&str] = &["um", "uh", "er", "erm", "ah", "hmm", "mm", "uhh", "umm"];
const FILLER_PAIRS: &[(&str, &str)] = &[
    ("you", "know"),
    ("i", "mean"),
    ("sort", "of"),
    ("kind", "of"),
];
const FILLER_WORDS: &[&str] = &[
    "like",
    "so",
    "right",
    "well",
    "okay",
    "ok",
    "actually",
    "basically",
];

fn distance(a: &str, b: &str) -> usize {
    let a: Vec<char> = a.chars().collect();
    let b: Vec<char> = b.chars().collect();
    let mut row: Vec<usize> = (0..=b.len()).collect();
    for i in 1..=a.len() {
        let mut previous = row[0];
        row[0] = i;
        for j in 1..=b.len() {
            let cost = usize::from(a[i - 1] != b[j - 1]);
            let value = (previous + cost).min(row[j] + 1).min(row[j - 1] + 1);
            previous = row[j];
            row[j] = value;
        }
    }
    row[b.len()]
}

fn similar(a: &str, b: &str) -> bool {
    let longest = a.chars().count().max(b.chars().count());
    longest > 0 && distance(a, b) * 5 <= longest * 2
}

fn contains_run(haystack: &[Token], needle: &[&Token]) -> bool {
    haystack.len() >= needle.len()
        && haystack
            .windows(needle.len())
            .any(|w| w.iter().zip(needle).all(|(a, b)| a.key == b.key))
}

/// Align source and output word by word and classify every difference.
pub fn account(source: &str, output: &str) -> Ledger {
    let a = tokens(source);
    let b = tokens(output);
    let mut ledger = Ledger {
        source_words: a.len() as u32,
        output_words: b.len() as u32,
        ..Default::default()
    };
    let mut prefix = 0;
    while prefix < a.len() && prefix < b.len() && a[prefix].key == b[prefix].key {
        prefix += 1;
    }
    let mut suffix = 0;
    while suffix < a.len() - prefix
        && suffix < b.len() - prefix
        && a[a.len() - 1 - suffix].key == b[b.len() - 1 - suffix].key
    {
        suffix += 1;
    }
    let (n, m) = (a.len() - prefix - suffix, b.len() - prefix - suffix);
    if n.saturating_mul(m) > 16_000_000 {
        // Too large to align. The caller treats an unmeasured account as a refusal.
        return ledger;
    }
    ledger.measured = true;
    ledger.kept = (prefix + suffix) as u32;
    let width = m + 1;
    let mut table = vec![0u32; (n + 1) * width];
    for (j, cell) in table.iter_mut().enumerate().take(m + 1) {
        *cell = j as u32;
    }
    for i in 1..=n {
        table[i * width] = i as u32;
        for j in 1..=m {
            let same = a[prefix + i - 1].key == b[prefix + j - 1].key;
            let diagonal = table[(i - 1) * width + j - 1] + u32::from(!same);
            let up = table[(i - 1) * width + j] + 1;
            let left = table[i * width + j - 1] + 1;
            table[i * width + j] = diagonal.min(up).min(left);
        }
    }
    // Walk back, collecting runs of differences between matched words.
    let mut hunks: Vec<(Vec<usize>, Vec<usize>)> = Vec::new();
    let (mut i, mut j) = (n, m);
    let mut open: Option<(Vec<usize>, Vec<usize>)> = None;
    while i > 0 || j > 0 {
        let here = table[i * width + j];
        let same = i > 0 && j > 0 && a[prefix + i - 1].key == b[prefix + j - 1].key;
        if same && here == table[(i - 1) * width + j - 1] {
            if let Some(hunk) = open.take() {
                hunks.push(hunk);
            }
            ledger.kept += 1;
            i -= 1;
            j -= 1;
            continue;
        }
        let hunk = open.get_or_insert_with(Default::default);
        if i > 0 && j > 0 && here == table[(i - 1) * width + j - 1] + 1 {
            hunk.0.push(prefix + i - 1);
            hunk.1.push(prefix + j - 1);
            i -= 1;
            j -= 1;
        } else if i > 0 && here == table[(i - 1) * width + j] + 1 {
            hunk.0.push(prefix + i - 1);
            i -= 1;
        } else {
            hunk.1.push(prefix + j - 1);
            j -= 1;
        }
    }
    hunks.extend(open);
    let text_of = |text: &str, tokens: &[Token], ids: &[usize]| -> String {
        match (ids.first(), ids.last()) {
            (Some(&first), Some(&last)) => text[tokens[first].start..tokens[last].end].to_owned(),
            _ => String::new(),
        }
    };
    fn note(ledger: &mut Ledger, kind: &str, before: String, after: String) {
        if ledger.changes.len() < 60 {
            ledger.changes.push(Change {
                kind: kind.into(),
                before,
                after,
            });
        }
    }
    for (mut from, mut to) in hunks.into_iter().rev() {
        from.reverse();
        to.reverse();
        let key = |tokens: &[Token], ids: &[usize]| -> String {
            ids.iter().map(|&k| tokens[k].key.as_str()).collect()
        };
        // A run of source words with nothing in their place.
        let dropped = |ledger: &mut Ledger, ids: &[usize]| {
            let mut plain: Vec<usize> = Vec::new();
            let mut k = 0;
            while k < ids.len() {
                let id = ids[k];
                let word = a[id].key.as_str();
                let pair = ids.get(k + 1).is_some_and(|&next| {
                    next == id + 1 && FILLER_PAIRS.contains(&(word, a[next].key.as_str()))
                });
                let stutter = (id > 0 && a[id - 1].key == word)
                    || a.get(id + 1).is_some_and(|next| next.key == word);
                if pair {
                    ledger.fillers_removed += 2;
                    k += 2;
                } else if FILLERS.contains(&word) || FILLER_WORDS.contains(&word) || stutter {
                    ledger.fillers_removed += 1;
                    k += 1;
                } else {
                    plain.push(id);
                    k += 1;
                }
            }
            if plain.is_empty() {
                return;
            }
            let run: Vec<&Token> = plain.iter().map(|&k| &a[k]).collect();
            if run.len() >= 3 && contains_run(&b, &run) {
                ledger.repeats_removed += run.len() as u32;
                return;
            }
            ledger.removed += run.len() as u32;
            ledger.longest_removed = ledger.longest_removed.max(run.len() as u32);
            note(
                ledger,
                "removed",
                text_of(source, &a, &plain),
                String::new(),
            );
        };
        let added = |ledger: &mut Ledger, ids: &[usize]| {
            if ids.is_empty() {
                return;
            }
            ledger.inserted += ids.len() as u32;
            ledger.longest_inserted = ledger.longest_inserted.max(ids.len() as u32);
            if ids.len() >= 2 {
                note(ledger, "inserted", String::new(), text_of(output, &b, ids));
            }
        };
        if to.is_empty() {
            dropped(&mut ledger, &from);
            continue;
        }
        if from.is_empty() {
            added(&mut ledger, &to);
            continue;
        }
        // A difference often mixes one repaired word with removed debris beside
        // it. Find the part of the source run that the output run repairs.
        let target = key(&b, &to);
        let mut matched: Option<(usize, usize, bool)> = None;
        'search: for exact in [true, false] {
            for size in (1..=from.len().min(to.len() + 2)).rev() {
                for at in 0..=from.len() - size {
                    let candidate = key(&a, &from[at..at + size]);
                    if (exact && candidate == target) || (!exact && similar(&candidate, &target)) {
                        matched = Some((at, size, exact));
                        break 'search;
                    }
                }
            }
        }
        if let Some((at, size, exact)) = matched {
            if exact && (size != 1 || to.len() != 1) {
                ledger.joined += size as u32;
            } else {
                ledger.respelled += size as u32;
            }
            dropped(&mut ledger, &from[..at]);
            dropped(&mut ledger, &from[at + size..]);
            continue;
        }
        let paired = from.len().min(to.len());
        ledger.replaced += paired as u32;
        note(
            &mut ledger,
            "replaced",
            text_of(source, &a, &from),
            text_of(output, &b, &to),
        );
        if from.len() > paired {
            let extra = (from.len() - paired) as u32;
            ledger.removed += extra;
            ledger.longest_removed = ledger.longest_removed.max(extra);
        }
        if to.len() > paired {
            let extra = (to.len() - paired) as u32;
            ledger.inserted += extra;
            ledger.longest_inserted = ledger.longest_inserted.max(extra);
        }
    }
    // Numbers are compared as a multiset: a digit string may move, not vanish.
    let numbers = |tokens: &[Token]| -> Vec<String> {
        let mut list: Vec<String> = tokens
            .iter()
            .filter(|t| t.key.chars().any(|c| c.is_ascii_digit()))
            .map(|t| t.key.clone())
            .collect();
        list.sort();
        list
    };
    let (mut lost, mut added) = (numbers(&a), numbers(&b));
    let mut k = 0;
    while k < lost.len() {
        if let Some(at) = added.iter().position(|n| *n == lost[k]) {
            added.remove(at);
            lost.remove(k);
        } else {
            k += 1;
        }
    }
    lost.truncate(20);
    added.truncate(20);
    ledger.numbers_lost = lost;
    ledger.numbers_added = added;
    ledger
}

/// Length of the longest common subsequence of two key sequences.
fn common(a: &[&str], b: &[&str]) -> usize {
    let mut row = vec![0usize; b.len() + 1];
    for x in a {
        let mut diagonal = 0;
        for (j, y) in b.iter().enumerate() {
            let above = row[j + 1];
            row[j + 1] = if x == y {
                diagonal + 1
            } else {
                above.max(row[j])
            };
            diagonal = above;
        }
    }
    row[b.len()]
}

/// A response sometimes runs on into the read-only text that follows its
/// passage, or opens with the text that precedes it. Such a run is recognized
/// by its words, which are those of the neighbouring text in the same order,
/// and is cut off, provided the account then shows fewer added words.
/// Returns the output that remains and its account.
pub fn settle(before: &str, passage: &str, output: &str, after: &str) -> (String, Ledger) {
    const LEAST: usize = 8;
    let mut text = output.to_owned();
    let mut ledger = account(passage, &text);
    if ledger.longest_inserted < LEAST as u32 {
        return (text, ledger);
    }
    let cost = |ledger: &Ledger| ledger.inserted + ledger.removed + ledger.replaced;
    // The end of the output against the start of the text that follows.
    let context = tokens(after);
    let context: Vec<&str> = context.iter().take(80).map(|t| t.key.as_str()).collect();
    let words = tokens(&text);
    let keys: Vec<&str> = words.iter().map(|t| t.key.as_str()).collect();
    if context.len() >= LEAST && keys.len() > LEAST {
        let first = keys.len().saturating_sub(context.len() + 20);
        // Of the places where the run may begin, take the one that leaves the
        // output closest to the passage.
        let best = (first..=keys.len() - LEAST)
            .filter(|&j| {
                context[..3.min(context.len())].contains(&keys[j])
                    && common(&keys[j..], &context) * 10 >= (keys.len() - j) * 7
            })
            .take(6)
            .map(|j| {
                let shorter = text[..words[j].start].trim_end().to_owned();
                let account = account(passage, &shorter);
                (cost(&account), j, shorter, account)
            })
            .min_by_key(|candidate| candidate.0);
        if let Some((_, j, shorter, account)) = best
            && ledger.inserted >= account.inserted + ((keys.len() - j) * 7 / 10) as u32
        {
            text = shorter;
            ledger = account;
        }
    }
    // The start of the output against the end of the text that precedes.
    let context = tokens(before);
    let skip = context.len().saturating_sub(80);
    let context: Vec<&str> = context[skip..].iter().map(|t| t.key.as_str()).collect();
    let words = tokens(&text);
    let keys: Vec<&str> = words.iter().map(|t| t.key.as_str()).collect();
    if context.len() >= LEAST && keys.len() > LEAST {
        let last = keys.len().min(context.len() + 20).min(words.len() - 1);
        let best = (LEAST..=last)
            .rev()
            .filter(|&j| {
                context[context.len() - 3.min(context.len())..].contains(&keys[j - 1])
                    && common(&keys[..j], &context) * 10 >= j * 7
            })
            .take(6)
            .map(|j| {
                // Keep any Markdown mark that opens the first kept word's line.
                let line = text[..words[j].start].rfind('\n').map_or(0, |at| at + 1);
                let marks_only = text[line..words[j].start]
                    .chars()
                    .all(|c| !c.is_alphanumeric());
                let start = if marks_only {
                    line.max(words[j - 1].end)
                } else {
                    words[j].start
                };
                let shorter = text[start..].trim_start().to_owned();
                let account = account(passage, &shorter);
                (cost(&account), j, shorter, account)
            })
            .min_by_key(|candidate| candidate.0);
        if let Some((_, j, shorter, account)) = best
            && ledger.inserted >= account.inserted + (j * 7 / 10) as u32
        {
            text = shorter;
            ledger = account;
        }
    }
    (text, ledger)
}

/// Decide whether a response is an edit of its source. The test is of gross
/// loss and gross addition only. Repairs of words, numbers and quotations are
/// never a reason to refuse a passage; they are counted and, where an owner
/// would want to look, noted.
pub fn verdict(ledger: &Ledger) -> Verdict {
    if !ledger.measured {
        return Verdict::Reject("the passage is too large to account for".into());
    }
    if ledger.source_words > 0 && ledger.output_words == 0 {
        return Verdict::Reject("the response holds no words".into());
    }
    if ledger.longest_removed >= 25
        || (ledger.source_words >= 8 && ledger.removed * 2 >= ledger.source_words)
    {
        return Verdict::Reject(format!(
            "the response dropped {} of {} source words ({} in one run)",
            ledger.removed, ledger.source_words, ledger.longest_removed
        ));
    }
    if ledger.longest_inserted >= 25 || ledger.inserted > ledger.source_words / 2 + 8 {
        return Verdict::Reject(format!(
            "the response added {} words that are not in the source ({} in one run)",
            ledger.inserted, ledger.longest_inserted
        ));
    }
    let mut notes = Vec::new();
    if !ledger.numbers_lost.is_empty() || !ledger.numbers_added.is_empty() {
        notes.push(format!(
            "Numbers changed. In the source only: {}. In the result only: {}",
            if ledger.numbers_lost.is_empty() {
                "none".into()
            } else {
                ledger.numbers_lost.join(", ")
            },
            if ledger.numbers_added.is_empty() {
                "none".into()
            } else {
                ledger.numbers_added.join(", ")
            }
        ));
    }
    if notes.is_empty() {
        Verdict::Accept
    } else {
        Verdict::Review(notes)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_source_word_is_accounted_for_once_and_gross_loss_is_refused() {
        let source = "um so the the govern mint of the united states was was founded in 1787 and \
                      you know it has has endured their trials for more than 200 years as any one can see";
        let output = "The government of the United States was founded in 1787, and it has \
                      endured there trials for more than 200 years, as anyone can see.";
        let ledger = account(source, output);
        assert!(ledger.measured && ledger.accounted(), "{ledger:?}");
        assert_eq!(ledger.joined, 2, "any one -> anyone");
        assert_eq!(
            ledger.respelled, 3,
            "govern mint -> government, their -> there"
        );
        assert_eq!(ledger.fillers_removed, 7, "um, so, the, was, you know, has");
        assert_eq!(
            (ledger.removed, ledger.inserted, ledger.replaced),
            (0, 0, 0)
        );
        assert!(ledger.numbers_lost.is_empty() && ledger.numbers_added.is_empty());
        assert_eq!(verdict(&ledger), Verdict::Accept);

        // A summary is not an edit.
        let long: String = (0..120).map(|n| format!("word{n} ")).collect();
        let short: String = (0..40).map(|n| format!("word{n} ")).collect();
        let ledger = account(&long, &short);
        assert!(ledger.accounted());
        assert!(matches!(verdict(&ledger), Verdict::Reject(_)));
        // Neither is an answer written in place of the passage.
        let ledger = account(
            "what is the capital of france",
            &format!("What is the capital of France? {long}"),
        );
        assert!(matches!(verdict(&ledger), Verdict::Reject(_)));
        // A changed number is accepted and reported.
        let ledger = account(
            "it cost 1,250 dollars in 1994",
            "It cost 1250 dollars in 1984.",
        );
        assert_eq!(ledger.numbers_lost, ["1994"]);
        assert_eq!(ledger.numbers_added, ["1984"]);
        assert!(matches!(verdict(&ledger), Verdict::Review(_)));
        // A response that runs on into the text after its passage is cut back.
        // or opens with the text before it. A word of the passage that also
        // stands in the neighbouring text does not hide the run.
        let after: String = (0..40).map(|n| format!("later{n} mat ")).collect();
        let before: String = (0..40).map(|n| format!("earlier{n} ")).collect();
        let (text, ledger) = settle(
            &before,
            "the cat sat on the mat",
            &format!("{before}The cat sat on the mat. {after}"),
            &after,
        );
        assert_eq!(text, "The cat sat on the mat.");
        assert_eq!(verdict(&ledger), Verdict::Accept);
        assert!(unwrap("<think>x</think><clean>Done.").is_err());
        assert_eq!(unwrap("<clean>\nDone.\n</clean>").unwrap(), "Done.");
    }
}
