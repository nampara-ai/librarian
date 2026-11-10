//! Deterministic projections of the layout model.
use super::text::list_marker;
use super::*;

#[derive(Debug, Clone, Copy)]
pub struct RenderOptions {
    /// Include running headers, footers and page numbers.
    pub furniture: bool,
    /// Insert an HTML comment at each page boundary.
    pub page_markers: bool,
    /// Emit bold, italic, code and link markup inside text.
    pub styles: bool,
    /// Mark the position of pictures with an HTML comment.
    pub pictures: bool,
}

impl Default for RenderOptions {
    fn default() -> Self {
        Self {
            furniture: false,
            page_markers: false,
            styles: true,
            pictures: true,
        }
    }
}

impl RenderOptions {
    /// Text only: no markup beyond headings, lists and tables.
    pub fn plain() -> Self {
        Self {
            furniture: true,
            page_markers: false,
            styles: false,
            pictures: false,
        }
    }
}

fn escape_html(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

/// Apply inline spans to `text[from..]`. A span that covers the whole text
/// carries no information beyond the element's role and is dropped.
fn styled(text: &str, spans: &[Span], from: usize, options: RenderOptions) -> String {
    if !options.styles || spans.is_empty() {
        return text[from..].to_owned();
    }
    let whole = spans.len() == 1
        && spans[0].start as usize <= from
        && spans[0].end as usize >= text.len()
        && spans[0].link.is_none();
    if whole {
        return text[from..].to_owned();
    }
    let mut out = String::with_capacity(text.len() - from + spans.len() * 4);
    let mut cursor = from;
    for span in spans {
        let (start, end) = (
            (span.start as usize).max(from),
            (span.end as usize).min(text.len()),
        );
        if start >= end
            || start < cursor
            || !text.is_char_boundary(start)
            || !text.is_char_boundary(end)
        {
            continue;
        }
        out.push_str(&text[cursor..start]);
        let inner = &text[start..end];
        // Markers must hug the text they wrap.
        let lead = inner.len() - inner.trim_start().len();
        let trail = inner.len() - inner.trim_end().len();
        let core = &inner[lead..inner.len() - trail];
        if core.is_empty() {
            out.push_str(inner);
            cursor = end;
            continue;
        }
        let mut piece = if span.code {
            format!("`{core}`")
        } else {
            match (span.bold, span.italic) {
                (true, true) => format!("***{core}***"),
                (true, false) => format!("**{core}**"),
                (false, true) => format!("*{core}*"),
                (false, false) => core.to_owned(),
            }
        };
        if let Some(uri) = &span.link {
            piece = format!("[{piece}]({uri})");
        }
        out.push_str(&inner[..lead]);
        out.push_str(&piece);
        out.push_str(&inner[inner.len() - trail..]);
        cursor = end;
    }
    out.push_str(&text[cursor..]);
    out
}

fn table(element: &Element) -> String {
    let (rows, cols) = (element.rows as usize, element.cols as usize);
    if rows == 0 || cols == 0 {
        return element.text.clone();
    }
    let spanning = element.cells.iter().any(|c| c.rowspan > 1 || c.colspan > 1);
    if spanning {
        // Markdown tables cannot merge cells; HTML keeps the structure.
        let mut out = String::from("<table>\n");
        for row in 0..rows as u32 {
            out.push_str("<tr>");
            for cell in element.cells.iter().filter(|c| c.row == row) {
                let tag = if cell.header { "th" } else { "td" };
                out.push('<');
                out.push_str(tag);
                if cell.rowspan > 1 {
                    out.push_str(&format!(" rowspan=\"{}\"", cell.rowspan));
                }
                if cell.colspan > 1 {
                    out.push_str(&format!(" colspan=\"{}\"", cell.colspan));
                }
                out.push('>');
                out.push_str(&escape_html(&cell.text).replace('\n', "<br>"));
                out.push_str(&format!("</{tag}>"));
            }
            out.push_str("</tr>\n");
        }
        out.push_str("</table>");
        return out;
    }
    let mut grid = vec![vec![String::new(); cols]; rows];
    for cell in &element.cells {
        if let Some(slot) = grid
            .get_mut(cell.row as usize)
            .and_then(|r| r.get_mut(cell.col as usize))
        {
            if !slot.is_empty() {
                slot.push(' ');
            }
            slot.push_str(&cell.text.replace('|', "\\|").replace('\n', "<br>"));
        }
    }
    let mut out = String::new();
    for (index, row) in grid.iter().enumerate() {
        out.push_str("| ");
        out.push_str(&row.join(" | "));
        out.push_str(" |\n");
        if index == 0 {
            out.push('|');
            out.push_str(&" --- |".repeat(cols));
            out.push('\n');
        }
    }
    out.trim_end().to_owned()
}

/// Reader Markdown. Every body element appears exactly once, in reading order.
pub fn markdown(document: &Document, options: RenderOptions) -> String {
    let mut out = String::new();
    let mut page = 0;
    let mut joining = false;
    let mut tight = false;
    for element in &document.elements {
        if element.role == Role::Hidden || (element.role.furniture() && !options.furniture) {
            continue;
        }
        if options.page_markers && element.page != page {
            page = element.page;
            if !joining {
                out.push_str(&format!("<!-- page {page} -->\n\n"));
            }
        }
        let text = element.text.as_str();
        let body = |from: usize| styled(text, &element.spans, from, options);
        let piece = match element.role {
            Role::Title => format!("# {}", text.trim()),
            Role::Heading => format!(
                "{} {}",
                "#".repeat(element.level.unwrap_or(1).clamp(1, 6) as usize),
                text.trim()
            ),
            Role::ListItem => match list_marker(text) {
                Some(marker) => {
                    let head = text[..marker].trim();
                    if head
                        .chars()
                        .next()
                        .is_some_and(|c| c.is_alphanumeric() || c == '(')
                    {
                        // Markdown renumbers "1." lists; keep the printed label.
                        format!("- {head} {}", body(marker))
                    } else {
                        format!("- {}", body(marker))
                    }
                }
                None => format!("- {}", body(0)),
            },
            Role::Code => format!("```\n{text}\n```"),
            Role::Table => table(element),
            Role::Picture => {
                let mut piece = String::new();
                if options.pictures {
                    piece = format!(
                        "<!-- picture: page {}, x {:.0} y {:.0} w {:.0} h {:.0} -->",
                        element.page,
                        element.bbox.x,
                        element.bbox.y,
                        element.bbox.w,
                        element.bbox.h
                    );
                }
                if !text.trim().is_empty() {
                    if !piece.is_empty() {
                        piece.push_str("\n\n");
                    }
                    piece.push_str(&text.replace('\n', "  \n"));
                }
                if piece.is_empty() {
                    continue;
                }
                piece
            }
            _ => body(0),
        };
        if joining {
            if !tight {
                out.push(' ');
            }
            out.push_str(&piece);
        } else {
            if !out.is_empty() {
                out.push_str("\n\n");
            }
            out.push_str(&piece);
        }
        joining = element.continues;
        tight = element.tight;
    }
    out.push('\n');
    out
}
