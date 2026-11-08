//! Format-independent layout model: positioned words in, typed elements out.
//!
//! Miners (PDFium, OCR, Office XML, HTML) emit [`DocumentInput`]. Deterministic
//! reconstruction turns it into a [`Document`] of ordered, typed elements with a
//! per-page routing decision that says where intelligence is actually needed.
//! Coordinates are PDF points with a top-left origin. No I/O happens here.
use serde::{Deserialize, Serialize};

mod reconstruct;
mod render;

pub use reconstruct::reconstruct;
pub use render::{RenderOptions, markdown};

pub const SCHEMA: &str = "librarian-layout-v1";

#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
pub struct Rect {
    pub x: f32,
    pub y: f32,
    pub w: f32,
    pub h: f32,
}

impl Rect {
    pub fn new(x: f32, y: f32, w: f32, h: f32) -> Self {
        Self { x, y, w, h }
    }
    pub fn right(&self) -> f32 {
        self.x + self.w
    }
    pub fn bottom(&self) -> f32 {
        self.y + self.h
    }
    pub fn cx(&self) -> f32 {
        self.x + self.w / 2.0
    }
    pub fn cy(&self) -> f32 {
        self.y + self.h / 2.0
    }
    pub fn area(&self) -> f32 {
        self.w.max(0.0) * self.h.max(0.0)
    }
    pub fn union(&self, other: &Rect) -> Rect {
        let x = self.x.min(other.x);
        let y = self.y.min(other.y);
        Rect::new(
            x,
            y,
            self.right().max(other.right()) - x,
            self.bottom().max(other.bottom()) - y,
        )
    }
    pub fn overlap_y(&self, other: &Rect) -> f32 {
        (self.bottom().min(other.bottom()) - self.y.max(other.y)).max(0.0)
    }
    pub fn overlap_x(&self, other: &Rect) -> f32 {
        (self.right().min(other.right()) - self.x.max(other.x)).max(0.0)
    }
    pub fn contains_center(&self, other: &Rect) -> bool {
        let (x, y) = (other.cx(), other.cy());
        x >= self.x && x <= self.right() && y >= self.y && y <= self.bottom()
    }
}

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
    Ocr,
    Model,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Word {
    pub text: String,
    pub bbox: Rect,
    /// Index into [`DocumentInput::fonts`].
    pub font: u32,
    /// Effective font size in points.
    pub size: f32,
    /// Source line identifier from the miner; a change always starts a new line.
    pub line: u32,
    /// Baseline y (top-left origin). Zero when the miner cannot report it.
    #[serde(default)]
    pub baseline: f32,
    /// The word ends with a line-break hyphen that is not part of the text.
    #[serde(default)]
    pub hyphen: bool,
    #[serde(default)]
    pub origin: Origin,
    #[serde(default = "full_confidence")]
    pub confidence: f32,
}

fn full_confidence() -> f32 {
    1.0
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
    /// Thin vector strokes/fills that can separate table rows and columns.
    pub rules: Vec<Rect>,
    /// Characters in the native text layer, and how many were undecodable.
    pub text_chars: u32,
    pub bad_chars: u32,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct DocumentInput {
    pub format: String,
    pub fonts: Vec<Font>,
    pub pages: Vec<PageInput>,
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
    Picture,
    Header,
    Footer,
    PageNumber,
}

impl Role {
    /// Page furniture is retained in the model but omitted from reader output.
    pub fn furniture(self) -> bool {
        matches!(self, Role::Header | Role::Footer | Role::PageNumber)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Cell {
    pub row: u32,
    pub col: u32,
    pub text: String,
    pub bbox: Rect,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub words: Vec<u32>,
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
    pub cells: Vec<Cell>,
    #[serde(default, skip_serializing_if = "is_zero")]
    pub rows: u32,
    #[serde(default, skip_serializing_if = "is_zero")]
    pub cols: u32,
    /// The paragraph continues in the next body element (often across a page break).
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub continues: bool,
    pub origin: Origin,
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

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Route {
    pub page: u32,
    pub tier: Tier,
    pub reasons: Vec<String>,
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
    /// Typical gap between ink boxes of consecutive lines, in points.
    pub line_gap: f32,
    /// Words not represented by any element. Must be zero.
    pub unassigned_words: u32,
    /// Words represented more than once. Must be zero.
    pub duplicated_words: u32,
    pub roles: std::collections::BTreeMap<String, u32>,
    pub tiers: std::collections::BTreeMap<String, u32>,
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
