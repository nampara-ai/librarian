use super::*;

pub fn source(
    library: &Library,
    job: i64,
    page: i64,
    kind: &str,
    parent: &str,
    start: usize,
    text: &str,
) -> Result<String> {
    let hash = digest(text);
    let id = format!(
        "{kind}:{page}:{start}:{}",
        &digest(&format!("{parent}\0{hash}"))[..20]
    );
    library.db.execute("INSERT OR IGNORE INTO editorial_sources(job,id,page,kind,parent,start,end,sha256,content) SELECT ?,?,?,?,?,?,?,?,? WHERE EXISTS(SELECT 1 FROM jobs WHERE id=? AND status='running')",params![job,id,page,kind,parent,start as i64,(start+text.len()) as i64,hash,text,job])?;
    if !parent.is_empty() {
        library.db.execute("INSERT OR IGNORE INTO editorial_source_links(job,child,parent,relation) VALUES(?,?,?,'derived_from')",params![job,id,parent])?;
    }
    Ok(id)
}

pub fn page_id(library: &Library, job: i64, page: i64) -> Result<String> {
    if let Some(id) = saved::<String>(library, job, "page-identity", &page.to_string())? {
        return Ok(id);
    }
    let (path, observation, part): (String, i64, i64) = library.db.query_row(
        "SELECT image,observation,part FROM editorial_pages WHERE job=? AND page=?",
        params![job, page],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
    )?;
    use sha2::{Digest, Sha256};
    let image_hash = format!("{:x}", Sha256::digest(std::fs::read(path)?));
    let document = library
        .job(job)?
        .document
        .context("Missing document identity")?;
    let id = source(
        library,
        job,
        page,
        "page_image",
        &document,
        0,
        &format!(
            "document={document}; observation={observation}; part={part}; image_sha256={image_hash}"
        ),
    )?;
    save(library, job, "page-identity", &page.to_string(), &id)?;
    Ok(id)
}

pub fn register_page(library: &Library, job: i64, page: i64) -> Result<Vec<String>> {
    let parent = page_id(library, job, page)?;
    let (native, ocr, visual, cleaned): (String, Option<String>, Option<String>, Option<String>) =
        library.db.query_row(
            "SELECT native,ocr,reading,cleaned FROM editorial_pages WHERE job=? AND page=?",
            params![job, page],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )?;
    let mut result = vec![source(library, job, page, "native", &parent, 0, &native)?];
    if let Some(ocr) = ocr {
        let recognized: crate::ocr::OcrResult = serde_json::from_str(&ocr)?;
        let text = recognized
            .lines
            .iter()
            .map(|l| l.text.as_str())
            .collect::<Vec<_>>()
            .join("\n");
        result.push(source(library, job, page, "ocr", &parent, 0, &text)?);
        // Keep OCR geometry addressable, not inferred from edited character offsets.
        result.push(source(
            library,
            job,
            page,
            "ocr_geometry",
            &parent,
            0,
            &ocr,
        )?);
    }
    if let Some(visual) = visual {
        result.push(source(library, job, page, "visual", &parent, 0, &visual)?);
    }
    if let Some(cleaned) = cleaned {
        result.push(source(library, job, page, "cleaned", &parent, 0, &cleaned)?);
    }
    Ok(result)
}

pub fn excerpt(text: &str, start: usize, length: usize) -> (usize, String) {
    let mut start = start.min(text.len());
    while !text.is_char_boundary(start) {
        start += 1;
    }
    let mut end = start.saturating_add(length).min(text.len());
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    (start, text[start..end].into())
}

pub fn get(
    library: &Library,
    job: i64,
    id: &str,
    start: usize,
    length: usize,
) -> Result<Option<Value>> {
    let row:Option<(i64,String,String,i64,i64,String,String)>=library.db.query_row("SELECT page,kind,parent,start,end,sha256,content FROM editorial_sources WHERE job=? AND id=?",params![job,id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?,r.get(6)?))).optional()?;
    let image_id = if let Some(row) = &row {
        Some(page_id(library, job, row.0)?)
    } else {
        None
    };
    Ok(row.map(|(page,kind,parent,source_start,source_end,hash,text)| {
        let (offset,preview)=excerpt(&text,start,length.min(6000));
        json!({"source_span_id":id,"page":page,"page_image_id":image_id,"kind":kind,"parent_id":parent,"source_start":source_start,"source_end":source_end,"sha256":hash,"text":preview,"offset":offset,"total_bytes":text.len(),"truncated":offset>0||offset+preview.len()<text.len()})
    }))
}

pub fn page_packet(library: &Library, job: i64, page: i64) -> Result<Vec<Value>> {
    let ids = register_page(library, job, page)?;
    ids.iter()
        .map(|id| get(library, job, id, 0, 1800)?.context("Missing registered source"))
        .collect()
}

pub fn brief(library: &Library, job: i64, config: &Config) -> Result<Value> {
    let state = library.job(job)?;
    let format: String =
        library
            .db
            .query_row("SELECT format FROM job_inputs WHERE job=?", [job], |r| {
                r.get(0)
            })?;
    let provisional = saved::<Metadata>(library, job, "classification", "provisional")?;
    let mut facts = BTreeMap::new();
    let mut speakers = Vec::new();
    let mut structure = Vec::new();
    let mut query=library.db.prepare("SELECT category,key,value,unit FROM editorial_ledger WHERE job=? AND category IN ('subject','purpose','language','speakers','section') ORDER BY CASE stage WHEN 'reviewer' THEN 0 WHEN 'typesetter' THEN 1 ELSE 2 END,updated DESC LIMIT 32")?;
    for row in query.query_map([job], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, String>(2)?,
            r.get::<_, String>(3)?,
        ))
    })? {
        let (category, key, value, unit) = row?;
        let item = json!({"name":key,"description":value,"basis_unit":unit});
        match category.as_str() {
            "speakers" => speakers.push(item),
            "section" => structure.push(item),
            _ => {
                facts.entry(category).or_insert(value);
            }
        }
    }
    Ok(
        json!({"document_id":state.document,"source_name":Path::new(&state.source).file_name().map(|s|s.to_string_lossy()),"source_formats":[format],"subject":facts.get("subject").cloned().or_else(||provisional.as_ref().map(|m|m.summary.clone())),"purpose":facts.get("purpose"),"language":facts.get("language"),"author_or_speakers":speakers,"intended_readership":"An intelligent reader unfamiliar with the original source; the prose must also make sense aloud.","intended_voice":"Preserve the source's register and personality.","house_style":config.editorial_house_style(),"terminology":config.processing.glossary,"provisional_catalog":provisional,"provisional_structure":structure,"known_source_limitations":"Unknown fields are not established facts. Consult source evidence and the continuity ledger; correct provisional metadata when the source warrants it."}),
    )
}

pub fn ledger(library: &Library, job: i64, stage: &str, passage: &str) -> Result<Vec<Value>> {
    let mut query=library.db.prepare("SELECT category,key,value,unit FROM editorial_ledger WHERE job=? AND (stage=? OR stage='copyeditor' OR (?='reviewer' AND stage='typesetter')) ORDER BY CASE WHEN stage=? THEN 0 ELSE 1 END,updated DESC")?;
    let rows = query.query_map(params![job, stage, stage, stage], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, String>(2)?,
            r.get::<_, String>(3)?,
        ))
    })?;
    let mut relevant = Vec::new();
    let mut recent = Vec::new();
    let lower = passage.to_lowercase();
    let mut included = BTreeSet::new();
    for row in rows {
        let (category, key, value, unit) = row?;
        if !included.insert((category.clone(), key.clone())) {
            continue;
        }
        let entry = json!({"category":category,"key":key,"value":value,"basis_unit":unit});
        if lower.contains(&key.to_lowercase()) && relevant.len() < 24 {
            relevant.push(entry);
        } else if recent.len() < 12 {
            recent.push(entry);
        }
    }
    relevant.extend(recent);
    let mut bytes = 0;
    relevant.retain(|v| {
        bytes += v.to_string().len();
        bytes < 8000
    });
    Ok(relevant)
}

pub fn prior(library: &Library, job: i64, before: i64) -> Result<Vec<Value>> {
    let mut stmt=library.db.prepare("SELECT unit,payload FROM editorial_stages WHERE job=? AND stage='review' AND CAST(unit AS INTEGER)>0 AND CAST(unit AS INTEGER)<? ORDER BY CAST(unit AS INTEGER) DESC LIMIT 4")?;
    let rows = stmt
        .query_map(params![job, before], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    rows.into_iter().rev().map(|(unit,raw)| {
        let h:Handoff=serde_json::from_str(&raw)?;
        Ok(json!({"segment_id":format!("review:{unit}"),"expected_hash":digest(&h.emitted),"text":editorial::tail(&h.emitted,1200),"total_bytes":h.emitted.len(),"truncated":h.emitted.chars().count()>1200,"read_only":true}))
    }).collect()
}

pub fn following(library: &Library, job: i64, page: i64, review: bool) -> Result<String> {
    if review
        && let Some(next) = saved::<Handoff>(library, job, "typeset", &(page + 1).to_string())?
    {
        return Ok(next.emitted.chars().take(1200).collect());
    }
    let text: Option<String> = library
        .db
        .query_row(
            "SELECT native FROM editorial_pages WHERE job=? AND page=?",
            params![job, page + 1],
            |r| r.get(0),
        )
        .optional()?;
    Ok(text.unwrap_or_default().chars().take(1200).collect())
}

pub fn fetch(
    library: &Library,
    job: i64,
    before: i64,
    request: &contract::EvidenceRequest,
) -> Result<Value> {
    if request.kind == "prior_passage" {
        let page = request
            .id
            .strip_prefix("review:")
            .and_then(|s| s.parse::<i64>().ok())
            .filter(|n| *n > 0 && *n < before)
            .context("Only earlier review passages are addressable")?;
        let h = saved::<Handoff>(library, job, "review", &page.to_string())?
            .context("Review passage unavailable")?;
        let (offset, text) = excerpt(&h.emitted, request.start, request.length.clamp(1, 6000));
        let page = page.min(counts(library, job)?.0 as i64);
        let mut stmt=library.db.prepare("SELECT id,sha256,content FROM editorial_sources WHERE job=? AND page=? AND kind='artifact' LIMIT 32")?;
        let artifacts=stmt.query_map(params![job,page],|r| {
            let content:String=r.get(2)?;
            Ok(json!({"artifact_id":r.get::<_,String>(0)?,"sha256":r.get::<_,String>(1)?,"read_only_representation":excerpt(&content,0,180).1,"total_bytes":content.len(),"truncated":content.len()>180}))
        })?.collect::<rusqlite::Result<Vec<_>>>()?;
        return Ok(
            json!({"segment_id":request.id,"source_span_id":request.id,"expected_hash":digest(&h.emitted),"text":text,"offset":offset,"total_bytes":h.emitted.len(),"truncated":offset>0||offset+text.len()<h.emitted.len(),"read_only":true,"page_image_id":page_id(library,job,page)?,"source_evidence_ids":register_page(library,job,page)?,"protected_artifacts":artifacts}),
        );
    }
    get(
        library,
        job,
        &request.id,
        request.start,
        request.length.clamp(1, 6000),
    )?
    .context("Evidence ID unavailable in this job")
}

pub fn image_path(library: &Library, job: i64, id: &str) -> Result<String> {
    Ok(library.db.query_row("SELECT p.image FROM editorial_sources s JOIN editorial_pages p ON p.job=s.job AND p.page=s.page WHERE s.job=? AND s.id=? AND s.kind='page_image'",params![job,id],|r|r.get(0))?)
}
