//! Deterministic projections of the layout model.
use super::*;

#[derive(Debug, Clone, Copy, Default)]
pub struct RenderOptions {
    /// Include running headers, footers and page numbers.
    pub furniture: bool,
    /// Insert an HTML comment at each page boundary.
    pub page_markers: bool,
}

fn escape_cell(text: &str) -> String {
    text.replace('|', "\\|").replace('\n', "<br>")
}

fn table(element: &Element) -> String {
    let (rows, cols) = (element.rows as usize, element.cols as usize);
    if rows == 0 || cols == 0 {
        return element.text.clone();
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
            slot.push_str(&escape_cell(&cell.text));
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
    for element in &document.elements {
        if element.role.furniture() && !options.furniture {
            continue;
        }
        if options.page_markers && element.page != page {
            page = element.page;
            if !joining {
                out.push_str(&format!("<!-- page {page} -->\n\n"));
            }
        }
        let text = element.text.trim();
        let piece = match element.role {
            Role::Title => format!("# {text}"),
            Role::Heading => format!(
                "{} {text}",
                "#".repeat(1 + element.level.unwrap_or(1).clamp(1, 5) as usize)
            ),
            Role::ListItem => {
                let marker = super::reconstruct::list_marker(text).unwrap_or(0);
                let head = text[..marker].trim();
                let rest = &text[marker..];
                if head.chars().next().is_some_and(|c| c.is_ascii_digit()) {
                    format!("{} {rest}", head.replace(')', "."))
                } else {
                    format!("- {rest}")
                }
            }
            Role::Code => format!("```\n{}\n```", element.text),
            Role::Table => table(element),
            Role::Picture => format!(
                "<!-- picture: page {}, x {:.0} y {:.0} w {:.0} h {:.0} -->",
                element.page, element.bbox.x, element.bbox.y, element.bbox.w, element.bbox.h
            ),
            _ => text.to_owned(),
        };
        if joining {
            if out.ends_with('-') {
                out.pop();
            } else {
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
    }
    out.push('\n');
    out
}
