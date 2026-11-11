//! Format-independent layout model: positioned words in, typed elements out.
//!
//! Miners (PDFium, OCR, Office XML, HTML) emit [`DocumentInput`]. Deterministic
//! reconstruction turns it into a [`Document`] of ordered, typed elements with a
//! per-page routing decision that says where intelligence is actually needed.
//! Coordinates are PDF points with a top-left origin. No I/O happens here.
use serde::{Deserialize, Serialize};

mod blocks;
mod geom;
mod lattice;
mod reconstruct;
mod regions;
mod render;
mod tables;
#[cfg(test)]
mod tests;
mod text;

pub use geom::Rect;
pub use reconstruct::reconstruct;
pub use render::{RenderOptions, markdown};

pub const SCHEMA: &str = "librarian-layout-v2";

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Font {
    pub name: String,
    pub bold: bool,
    pub italic: bool,
    pub mono: bool,
}

/// How a word was obtained. Native text is decoded from the source file itself.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Origin {
    #[default]
    Native,
    /// Invisible text laid over a page image by an earlier OCR pass.
    OcrLayer,
    Ocr,
    Model,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Word {
    pub text: String,
    pub bbox: Rect,
    /// Index into [`DocumentInput::fonts`].
    pub font: u32,
    /// Effective font size in points.
    pub size: f32,
    /// Source line identifier from the miner; used only to break ties.
    pub line: u32,
    /// Baseline y (top-left origin). Zero when the miner cannot report it.
    #[serde(default)]
    pub baseline: f32,
    /// The word was cut by a line break at a hyphen. Whether the hyphen is
    /// part of the text is decided from the whole document.
    #[serde(default)]
    pub hyphen: bool,
    /// Drawn with an invisible render mode.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub hidden: bool,
    /// Not horizontal (axis labels, margin stamps, watermarks).
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub rotated: bool,
    #[serde(default)]
    pub origin: Origin,
    #[serde(default = "full_confidence")]
    pub confidence: f32,
}

fn full_confidence() -> f32 {
    1.0
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Link {
    pub rect: Rect,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub uri: Option<String>,
    /// One-based target page of an internal link.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub page: Option<u32>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct PageInput {
    pub number: u32,
    pub width: f32,
    pub height: f32,
    /// Words in source content order.
    pub words: Vec<Word>,
    /// Raster image placements.
    pub images: Vec<Rect>,
    /// Axis-aligned line segments: horizontal when `h` is ~0, vertical when `w` is ~0.
    pub rules: Vec<Rect>,
    /// Filled rectangles (cell shading, callout boxes).
    #[serde(default)]
    pub fills: Vec<Rect>,
    /// Bounds of vector artwork that is not a rule or a plain fill.
    #[serde(default)]
    pub drawings: Vec<Rect>,
    #[serde(default)]
    pub links: Vec<Link>,
    /// Characters in the native text layer, and how many were undecodable.
    pub text_chars: u32,
    pub bad_chars: u32,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct OutlineItem {
    pub level: u8,
    pub title: String,
    /// One-based page.
    pub page: u32,
    /// Target y on the page (top-left origin), when the destination has one.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub y: Option<f32>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct DocumentInput {
    pub format: String,
    pub fonts: Vec<Font>,
    pub pages: Vec<PageInput>,
    /// The author's own bookmarks: declared headings with levels.
    #[serde(default)]
    pub outline: Vec<OutlineItem>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Role {
    Title,
    Heading,
    Paragraph,
    ListItem,
    Table,
    Code,
    Caption,
    Footnote,
    Picture,
    Header,
    Footer,
    PageNumber,
    /// Text that is not visible on the page or not part of the reading flow.
    Hidden,
}

impl Role {
    /// Retained in the model but omitted from reader output.
    pub fn furniture(self) -> bool {
        matches!(
            self,
            Role::Header | Role::Footer | Role::PageNumber | Role::Hidden
        )
    }

    pub fn name(self) -> &'static str {
        match self {
            Role::Title => "title",
            Role::Heading => "heading",
            Role::Paragraph => "paragraph",
            Role::ListItem => "list_item",
            Role::Table => "table",
            Role::Code => "code",
            Role::Caption => "caption",
            Role::Footnote => "footnote",
            Role::Picture => "picture",
            Role::Header => "header",
            Role::Footer => "footer",
            Role::PageNumber => "page_number",
            Role::Hidden => "hidden",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Cell {
    pub row: u32,
    pub col: u32,
    #[serde(default = "one", skip_serializing_if = "is_one")]
    pub rowspan: u32,
    #[serde(default = "one", skip_serializing_if = "is_one")]
    pub colspan: u32,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub header: bool,
    pub text: String,
    pub bbox: Rect,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub words: Vec<u32>,
}

fn one() -> u32 {
    1
}

fn is_one(value: &u32) -> bool {
    *value == 1
}

/// Inline styling over a byte range of an element's text.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Span {
    pub start: u32,
    pub end: u32,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub bold: bool,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub italic: bool,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub code: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub link: Option<String>,
}

/// Where the evidence for an element's role came from.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Evidence {
    /// Geometry and typography measured on the document.
    #[default]
    Layout,
    /// Lines drawn in the file (table grids).
    Ruling,
    /// Declared by the author in the file's bookmarks.
    Declared,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Element {
    pub id: String,
    pub page: u32,
    pub role: Role,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub level: Option<u8>,
    pub text: String,
    pub bbox: Rect,
    /// Document-wide reading order.
    pub order: u32,
    pub font_size: f32,
    #[serde(default)]
    pub bold: bool,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub spans: Vec<Span>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub cells: Vec<Cell>,
    #[serde(default, skip_serializing_if = "is_zero")]
    pub rows: u32,
    #[serde(default, skip_serializing_if = "is_zero")]
    pub cols: u32,
    /// The element continues in the next body element (often across a page break).
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub continues: bool,
    /// When continuing, the next element completes a word: join without a space.
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub tight: bool,
    pub origin: Origin,
    #[serde(default)]
    pub evidence: Evidence,
    pub confidence: f32,
    /// Indices into the page's word list. Omitted from compact output.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub words: Vec<u32>,
}

fn is_zero(value: &u32) -> bool {
    *value == 0
}

/// Processing depth a page needs after deterministic reconstruction.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Tier {
    /// Native text and layout were reconstructed with confidence; no model needed.
    Deterministic,
    /// Structure is uncertain; a model should choose between explicit candidates.
    Review,
    /// Meaningful visual content needs a description from a vision model.
    Vision,
    /// No usable text layer; local OCR is required.
    Ocr,
}

impl Tier {
    pub fn name(self) -> &'static str {
        match self {
            Tier::Deterministic => "deterministic",
            Tier::Review => "review",
            Tier::Vision => "vision",
            Tier::Ocr => "ocr",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Route {
    pub page: u32,
    pub tier: Tier,
    pub reasons: Vec<String>,
    /// Regions that need the tier's attention; empty means the whole page.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub regions: Vec<Rect>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PageSummary {
    pub number: u32,
    pub width: f32,
    pub height: f32,
    pub words: u32,
    pub tier: Tier,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Stats {
    pub pages: u32,
    pub words: u32,
    pub elements: u32,
    pub body_font_size: f32,
    /// Typical baseline pitch as a multiple of font size.
    pub line_pitch: f32,
    /// Words not represented by any element. Must be zero.
    pub unassigned_words: u32,
    /// Words represented more than once. Must be zero.
    pub duplicated_words: u32,
    /// Non-space characters in the source words and in the element texts.
    /// `chars_out + hyphens_removed` must equal `chars_in`.
    pub chars_in: u64,
    pub chars_out: u64,
    pub hyphens_removed: u32,
    pub hyphens_kept: u32,
    pub roles: std::collections::BTreeMap<String, u32>,
    pub tiers: std::collections::BTreeMap<String, u32>,
}

impl Stats {
    /// Every word and every character of the source is accounted for exactly once.
    pub fn accounted(&self) -> bool {
        self.unassigned_words == 0
            && self.duplicated_words == 0
            && self.chars_in == self.chars_out + self.hyphens_removed as u64
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Document {
    pub schema: String,
    pub format: String,
    pub pages: Vec<PageSummary>,
    pub elements: Vec<Element>,
    pub routing: Vec<Route>,
    pub stats: Stats,
}

impl Document {
    /// Drop word indices for compact serialization.
    pub fn without_word_refs(mut self) -> Self {
        for element in &mut self.elements {
            element.words.clear();
            for cell in &mut element.cells {
                cell.words.clear();
            }
        }
        self
    }
}
