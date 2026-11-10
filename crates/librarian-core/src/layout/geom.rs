//! Rectangles and small numeric helpers. All comparisons are total orders.
use serde::{Deserialize, Serialize};

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
    pub fn from_edges(left: f32, top: f32, right: f32, bottom: f32) -> Self {
        Self::new(left, top, right - left, bottom - top)
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
    pub fn intersection_area(&self, other: &Rect) -> f32 {
        self.overlap_x(other) * self.overlap_y(other)
    }
    pub fn contains_point(&self, x: f32, y: f32) -> bool {
        x >= self.x && x <= self.right() && y >= self.y && y <= self.bottom()
    }
    pub fn contains_center(&self, other: &Rect) -> bool {
        self.contains_point(other.cx(), other.cy())
    }
    pub fn grow(&self, by: f32) -> Rect {
        Rect::new(
            self.x - by,
            self.y - by,
            self.w + 2.0 * by,
            self.h + 2.0 * by,
        )
    }
    pub fn finite(&self) -> bool {
        self.x.is_finite() && self.y.is_finite() && self.w.is_finite() && self.h.is_finite()
    }
}

pub(crate) fn union_all(rects: impl IntoIterator<Item = Rect>) -> Rect {
    rects
        .into_iter()
        .reduce(|a, b| a.union(&b))
        .unwrap_or_default()
}

pub(crate) fn median(values: &mut [f32]) -> f32 {
    quantile(values, 0.5)
}

/// `q` in `[0, 1]`; sorts the slice.
pub(crate) fn quantile(values: &mut [f32], q: f32) -> f32 {
    if values.is_empty() {
        return 0.0;
    }
    values.sort_by(f32::total_cmp);
    let index = ((values.len() - 1) as f32 * q).round() as usize;
    values[index.min(values.len() - 1)]
}

/// Group sorted positions whose neighbours are within `tolerance`; returns
/// `(mean, count)` per cluster in ascending order.
pub(crate) fn cluster(mut values: Vec<f32>, tolerance: f32) -> Vec<(f32, usize)> {
    values.retain(|v| v.is_finite());
    values.sort_by(f32::total_cmp);
    let mut clusters: Vec<(f32, usize)> = Vec::new();
    for value in values {
        match clusters.last_mut() {
            Some((mean, count)) if value - *mean <= tolerance => {
                *mean = (*mean * *count as f32 + value) / (*count as f32 + 1.0);
                *count += 1;
            }
            _ => clusters.push((value, 1)),
        }
    }
    clusters
}

/// Merge overlapping or touching `[start, end]` intervals.
pub(crate) fn merge_intervals(mut intervals: Vec<(f32, f32)>, slack: f32) -> Vec<(f32, f32)> {
    intervals.retain(|(a, b)| a.is_finite() && b.is_finite() && b >= a);
    intervals.sort_by(|a, b| a.0.total_cmp(&b.0));
    let mut merged: Vec<(f32, f32)> = Vec::new();
    for (start, end) in intervals {
        match merged.last_mut() {
            Some(last) if start <= last.1 + slack => last.1 = last.1.max(end),
            _ => merged.push((start, end)),
        }
    }
    merged
}

/// White intervals between merged ink intervals inside `[left, right]`.
pub(crate) fn free_of(ink: &[(f32, f32)], left: f32, right: f32) -> Vec<(f32, f32)> {
    let mut free = Vec::with_capacity(ink.len() + 1);
    let mut cursor = left;
    for &(start, end) in ink {
        if start > cursor {
            free.push((cursor, start));
        }
        cursor = cursor.max(end);
    }
    if right > cursor {
        free.push((cursor, right));
    }
    free
}

/// Pairwise intersection of two sorted interval lists, keeping pieces at
/// least `min` wide.
pub(crate) fn intersect(a: &[(f32, f32)], b: &[(f32, f32)], min: f32) -> Vec<(f32, f32)> {
    let mut out = Vec::new();
    let (mut i, mut j) = (0, 0);
    while i < a.len() && j < b.len() {
        let start = a[i].0.max(b[j].0);
        let end = a[i].1.min(b[j].1);
        if end - start >= min {
            out.push((start, end));
        }
        if a[i].1 < b[j].1 {
            i += 1;
        } else {
            j += 1;
        }
    }
    out
}
