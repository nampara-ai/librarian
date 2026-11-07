//! Evidence-aware editorial roles. v1 checkpoints continue through the original runner.
use super::*;
use librarian_core::editorial::{
    contract::{self, digest},
    protection::{self, Artifact},
};
use std::collections::{BTreeMap, BTreeSet};
mod audit;
mod evidence;
mod wire;

pub fn enabled(library: &Library, job: i64) -> Result<bool> {
    let version: String = library.db.query_row(
        "SELECT version FROM editorial_runs WHERE job=?",
        [job],
        |r| r.get(0),
    )?;
    match version.as_str() {
        "editorial-v1" => Ok(false),
        editorial::VERSION => Ok(true),
        _ => bail!(
            "Unsupported editorial run version {version}; retain its source and use a new run"
        ),
    }
}

pub fn freeze_contract(library: &Library, job: i64, config: &Config) -> Result<()> {
    let contract = json!({"version":editorial::VERSION,"copyeditor":{"version":editorial::COPYEDITOR_VERSION,"sha256":digest(editorial::COPYEDITOR)},"reviewer":{"version":editorial::REVIEWER_VERSION,"sha256":digest(editorial::REVIEWER)},"typesetter_sha256":digest(editorial::TYPESETTER_V2),"protocol_sha256":digest(contract::PROTOCOL),"house_style":config.editorial_house_style(),"terminology":config.processing.glossary});
    if let Some(old) = saved::<Value>(library, job, "contract", "roles")? {
        if old != contract {
            bail!(
                "This run's role contract differs from the installed version; retry as a new run to adopt changed prompts or house style"
            );
        }
    } else {
        save(library, job, "contract", "roles", &contract)?;
    }
    Ok(())
}

struct Unit {
    stage: &'static str,
    id: String,
    page: i64,
    text: String,
    context: Value,
    owned: Vec<String>,
    evidence: Vec<Value>,
    image: Option<Vec<u8>>,
    image_id: Option<String>,
}

fn model_limit(error: &anyhow::Error) -> bool {
    error.is::<OutputLimit>() || error.to_string().contains("context budget")
}

#[allow(clippy::too_many_arguments)]
async fn edit_chunks(
    library: &Library,
    job: i64,
    config: &Config,
    client: &ModelClient,
    stage: &'static str,
    page: i64,
    source: &str,
    context: Value,
) -> Result<String> {
    if source.trim().is_empty() {
        return Ok(source.into());
    }
    let protected = protection::protect(source);
    let mut windows: VecDeque<_> =
        editorial::ranges(&protected.text, config.processing.chunk_bytes).into();
    let actual_page = page.min(counts(library, job)?.0 as i64);
    let parent = evidence::source(
        library,
        job,
        actual_page,
        &format!("{stage}_input"),
        &evidence::page_id(library, job, actual_page)?,
        0,
        source,
    )?;
    let mut result = String::new();
    let mut page_evidence = evidence::page_packet(library, job, actual_page)?;
    if stage == "reviewer" && actual_page > 1 {
        page_evidence.extend(evidence::page_packet(library, job, actual_page - 1)?);
    }
    for evidence in &page_evidence {
        if let Some(id) = evidence["source_span_id"].as_str() {
            library.db.execute("INSERT OR IGNORE INTO editorial_source_links(job,child,parent,relation) VALUES(?,?,?,'supported_by')",params![job,parent,id])?;
        }
    }
    while let Some((start, end)) = windows.pop_front() {
        checkpoint(library, job)?;
        let masked = &protected.text[start..end];
        let owned_artifacts: Vec<_> = protected
            .artifacts
            .iter()
            .filter(|a| masked.contains(&a.marker))
            .cloned()
            .collect();
        let raw = protection::restore(masked, &owned_artifacts).map_err(anyhow::Error::msg)?;
        if raw.trim().is_empty() {
            result.push_str(&raw);
            continue;
        }
        let prefix_artifacts: Vec<_> = protected
            .artifacts
            .iter()
            .filter(|a| protected.text[..start].contains(&a.marker))
            .cloned()
            .collect();
        let raw_start = protection::restore(&protected.text[..start], &prefix_artifacts)
            .map_err(anyhow::Error::msg)?
            .len();
        let owned = evidence::source(
            library,
            job,
            actual_page,
            &format!("{stage}_window"),
            &parent,
            raw_start,
            &raw,
        )?;
        let mut context = context.clone();
        context["preceding_edited_in_this_passage"] = json!(editorial::tail(&result, 1200));
        context["following_assigned_source"] =
            json!(protected.text[end..].chars().take(900).collect::<String>());
        let unit = Unit {
            stage,
            id: format!("{page}:{raw_start}:{}", raw_start + raw.len()),
            page,
            text: raw,
            context,
            owned: vec![owned],
            evidence: page_evidence.clone(),
            image: None,
            image_id: None,
        };
        let unit_id = unit.id.clone();
        match wire::edit(library, job, config, client, unit).await {
            Ok(edited) => {
                if !result.is_empty() && !edited.is_empty() {
                    result.push_str(if protected.text[..start].ends_with("\n\n") {
                        "\n\n"
                    } else {
                        " "
                    });
                }
                result.push_str(&edited);
            }
            Err(error) if model_limit(&error) && end - start > 256 => {
                let smaller = editorial::ranges(masked, (end - start) / 2);
                if smaller.len() < 2 {
                    return Err(
                        error.context("An indivisible protected unit exceeds the model allowance")
                    );
                }
                library.db.execute(
                    "UPDATE editorial_owned SET status='split' WHERE job=? AND stage=? AND unit=?",
                    params![job, stage, unit_id],
                )?;
                for (a, b) in smaller.into_iter().rev() {
                    windows.push_front((start + a, start + b));
                }
                library.event(
                    job,
                    "info",
                    &format!("{stage}: continuing through smaller owned source spans"),
                )?;
            }
            Err(error) => return Err(error),
        }
    }
    Ok(result)
}

pub async fn copyeditor(
    library: &Library,
    job: i64,
    config: &Config,
    client: &ModelClient,
) -> Result<()> {
    let mut page = 1;
    let mut preceding = String::new();
    loop {
        checkpoint(library, job)?;
        let done = extraction_done(library, job)?;
        let row: Option<(String, Option<String>)> = library
            .db
            .query_row(
                "SELECT native,cleaned FROM editorial_pages WHERE job=? AND page=?",
                params![job, page],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?;
        let Some((native, existing)) = row else {
            if done {
                break;
            }
            tick(library, job).await?;
            continue;
        };
        let cleaned = if let Some(value) = existing {
            value
        } else if !config.processing.edit || native.trim().is_empty() {
            native
        } else {
            edit_chunks(library,job,config,client,"copyeditor",page,&native,json!({"preceding_committed":preceding,"following_source":evidence::following(library,job,page,false)?,"pending_unpublished":"","read_only_context":true})).await?
        };
        write_sidecar(library, job, page, "copyedited.md", &cleaned)?;
        library.db.execute("UPDATE editorial_pages SET cleaned=? WHERE job=? AND page=? AND EXISTS(SELECT 1 FROM jobs WHERE id=? AND status='running')",params![cleaned,job,page,job])?;
        if !cleaned.trim().is_empty() {
            preceding = editorial::tail(&cleaned, 1200);
        }
        page += 1;
    }
    Ok(())
}

pub async fn typesetter(
    library: &Library,
    job: i64,
    config: &Config,
    client: &ModelClient,
) -> Result<()> {
    let mut page = 1;
    let mut handoff = Handoff::default();
    loop {
        checkpoint(library, job)?;
        if let Some(prior) = saved::<Handoff>(library, job, "typeset", &page.to_string())? {
            handoff = prior;
            page += 1;
            continue;
        }
        let row:Option<(String,String,String)>=library.db.query_row("SELECT cleaned,reading,image FROM editorial_pages WHERE job=? AND page=? AND cleaned IS NOT NULL AND reading IS NOT NULL",params![job,page],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
        let Some((native, reading, path)) = row else {
            if extraction_done(library, job)? && page > counts(library, job)?.0 as i64 {
                break;
            }
            tick(library, job).await?;
            continue;
        };
        evidence::register_page(library, job, page)?;
        let parent = evidence::page_id(library, job, page)?;
        let mut pending = handoff.pending.clone();
        let mut stable = String::new();
        if config.processing.edit {
            let mut windows = VecDeque::from([(0, native.len(), 0, reading.len(), true)]);
            while let Some((a, b, c, d, whole)) = windows.pop_front() {
                let n = &native[a..b];
                let r = &reading[c..d];
                if n.trim().is_empty() && r.trim().is_empty() && pending.trim().is_empty() {
                    continue;
                }
                let mut owned = Vec::new();
                if !n.is_empty() {
                    owned.push(evidence::source(
                        library,
                        job,
                        page,
                        "typeset_native",
                        &parent,
                        a,
                        n,
                    )?);
                }
                if !r.is_empty() {
                    owned.push(evidence::source(
                        library,
                        job,
                        page,
                        "typeset_visual",
                        &parent,
                        c,
                        r,
                    )?);
                }
                if !pending.is_empty() {
                    owned.push(evidence::source(
                        library,
                        job,
                        page,
                        "typeset_pending",
                        &parent,
                        0,
                        &pending,
                    )?);
                }
                let mut supporting = Vec::new();
                for id in &owned {
                    if let Some(source) = evidence::get(library, job, id, 0, 1800)? {
                        supporting.push(source);
                    }
                }
                if whole {
                    supporting.extend(evidence::page_packet(library, job, page)?);
                }
                let image = if whole && config.processing.vision {
                    let mut bytes = std::io::Cursor::new(Vec::new());
                    model_image(Path::new(&path), config.processing.render_width)?
                        .write_to(&mut bytes, image::ImageFormat::Png)?;
                    Some(bytes.into_inner())
                } else {
                    None
                };
                let primary = if n.is_empty() { r } else { n };
                let unit = Unit {
                    stage: "typesetter",
                    id: format!("{page}:{a}:{b}:{c}:{d}:{}", &digest(&pending)[..12]),
                    page,
                    text: format!("{pending}{primary}"),
                    context: json!({"pending_unpublished":{"included_in_editable_passage":true,"prefix_bytes":pending.len()},"preceding_committed":editorial::tail(&format!("{}\n{stable}",handoff.context),6000),"following_source":evidence::following(library,job,page,false)?,"alternate_visual_reading_of_same_owned_region":if n.is_empty(){""}else{r},"whole_page":whole,"ownership":"Reconcile the SAME source region into ONE rendition. Never concatenate alternative readings. The pending prefix is assigned exactly once."}),
                    owned,
                    evidence: supporting,
                    image,
                    image_id: (whole && config.processing.vision).then_some(parent.clone()),
                };
                let unit_id = unit.id.clone();
                match wire::edit(library, job, config, client, unit).await {
                    Ok(text) => {
                        let next = protection::handoff(&text);
                        stable.push_str(&next.emitted);
                        pending = next.pending;
                    }
                    Err(error) if model_limit(&error) => {
                        // First keep the entire textual region and omit only the
                        // already-interpreted image. Then split at shared anchors.
                        if whole {
                            windows.push_front((a, b, c, d, false));
                            continue;
                        }
                        if n.len() + r.len() <= 256 {
                            return Err(error);
                        }
                        let split = divide_evidence(
                            library,
                            job,
                            client,
                            &EvidenceWindow {
                                native: n.into(),
                                reading: r.into(),
                                ocr: String::new(),
                                whole_page_image: false,
                            },
                        )
                        .await?;
                        let x = split[0].native.len();
                        let y = split[0].reading.len();
                        if [(n, x), (r, y)].iter().any(|(text, cut)| {
                            protection::protect(text)
                                .artifacts
                                .iter()
                                .any(|a| a.start < *cut && *cut < a.end)
                        }) {
                            return Err(error.context("The remaining aligned source cannot be divided without cutting a protected artifact; increase the model context/output allowance. Source and completed work are retained."));
                        }
                        if x + y == 0 || x + y == n.len() + r.len() {
                            return Err(error.context("Source alignment made no progress"));
                        }
                        library.db.execute("UPDATE editorial_owned SET status='split' WHERE job=? AND stage='typesetter' AND unit=?",params![job,unit_id])?;
                        windows.push_front((a + x, b, c + y, d, false));
                        windows.push_front((a, a + x, c, c + y, false));
                    }
                    Err(error) => return Err(error),
                }
            }
            handoff = protection::handoff(&format!("{stable}{pending}"));
        } else {
            handoff = protection::handoff(&format!(
                "{pending}\n\n{}",
                if reading.is_empty() {
                    &native
                } else {
                    &reading
                }
            ));
        }
        write_sidecar(
            library,
            job,
            page,
            "typeset.md",
            &format!("{}{}", handoff.emitted, handoff.pending),
        )?;
        save(library, job, "typeset", &page.to_string(), &handoff)?;
        library.progress(job, "assembling", page as u64, counts(library, job)?.0)?;
        page += 1;
    }
    save(library, job, "typeset", "final_tail", &handoff.pending)?;
    save(library, job, "typeset", "count", &(page - 1))?;
    Ok(())
}

pub async fn reviewer(
    library: &Library,
    job: i64,
    config: &Config,
    client: &ModelClient,
) -> Result<()> {
    let count =
        saved::<i64>(library, job, "typeset", "count")?.context("Missing Typesetter completion")?;
    let mut pending = String::new();
    for page in 1..=count + 1 {
        if let Some(previous) = saved::<Handoff>(library, job, "review", &page.to_string())? {
            pending = previous.pending;
            continue;
        }
        checkpoint(library, job)?;
        library.progress(
            job,
            "reviewing with evidence",
            page as u64 - 1,
            count as u64 + 1,
        )?;
        let source = if page <= count {
            saved::<Handoff>(library, job, "typeset", &page.to_string())?
                .context("Missing Typesetter passage")?
                .emitted
        } else {
            saved::<String>(library, job, "typeset", "final_tail")?.unwrap_or_default()
        };
        let owned = format!("{pending}{source}");
        let polished = if config.processing.edit {
            edit_chunks(library,job,config,client,"reviewer",page,&owned,json!({"pending_unpublished":{"included_in_editable_passage":true,"prefix_bytes":pending.len()},"following_source":evidence::following(library,job,page,true)?,"current_section":page,"total_sections":count+1})).await?
        } else {
            owned
        };
        let next = if page == count + 1 {
            Handoff {
                emitted: polished,
                pending: String::new(),
                context: String::new(),
            }
        } else {
            protection::handoff(&polished)
        };
        pending = next.pending.clone();
        save(library, job, "review", &page.to_string(), &next)?;
    }
    let unfinished: i64 = library.db.query_row(
        "SELECT count(*) FROM editorial_owned WHERE job=? AND status='planned'",
        [job],
        |r| r.get(0),
    )?;
    if unfinished > 0 {
        bail!(
            "{unfinished} source ownership records have not completed; committed results are retained for resume"
        );
    }
    super::materialize_review(library, job, count)?;
    audit::write_report(library, job)?;
    Ok(())
}

pub fn feedback(
    library: &Library,
    job: i64,
    segment: &str,
    replacement: &str,
    reason: &str,
) -> Result<i64> {
    if reason.trim().is_empty() {
        bail!("An editorial reason is required");
    }
    let number = segment
        .strip_prefix("review:")
        .and_then(|s| s.parse::<i64>().ok())
        .filter(|n| *n > 0)
        .context("Use a review:N segment ID")?;
    let model = saved::<Handoff>(library, job, "review", &number.to_string())?
        .context("Review segment not found")?;
    let mut source = Vec::new();
    for page in (number - 1).max(1)..=number.min(counts(library, job)?.0 as i64) {
        let mut stmt = library.db.prepare("SELECT id,kind,sha256,content FROM editorial_sources WHERE job=? AND page=? AND kind IN ('native','ocr','visual','page_image')")?;
        let values = stmt.query_map(params![job,page], |r| Ok(json!({"source_span_id":r.get::<_,String>(0)?,"kind":r.get::<_,String>(1)?,"sha256":r.get::<_,String>(2)?,"content":r.get::<_,String>(3)?,"page":page})))?.collect::<rusqlite::Result<Vec<_>>>()?;
        source.extend(values);
    }
    library.db.execute("INSERT INTO editorial_feedback(job,segment,original_source,model_text,corrected_text,reason,created) VALUES(?,?,?,?,?,?,?)",params![job,segment,serde_json::to_string(&json!({"document_id":library.job(job)?.document,"source_evidence":source}))?,model.emitted,replacement,reason,crate::store::now()])?;
    Ok(library.db.last_insert_rowid())
}

pub fn inspect(library: &Library, job: i64) -> Result<Value> {
    let query = |sql: &str| -> Result<Vec<Value>> {
        let mut stmt = library.db.prepare(sql)?;
        let raw = stmt
            .query_map([job], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        raw.into_iter()
            .map(|s| serde_json::from_str(&s).map_err(Into::into))
            .collect()
    };
    let mut reviewed = query(
        "SELECT json_object('segment_id','review:'||unit,'markdown',json_extract(payload,'$.emitted'),'pending_unpublished',json_extract(payload,'$.pending')) FROM editorial_stages WHERE job=? AND stage='review' AND CAST(unit AS INTEGER)>0 ORDER BY CAST(unit AS INTEGER)",
    )?;
    for segment in &mut reviewed {
        segment["sha256"] = json!(digest(segment["markdown"].as_str().unwrap_or_default()));
    }
    Ok(
        json!({"job":job,"contract":saved::<Value>(library,job,"contract","roles")?,"reviewed_segments":reviewed,"request_packets":query("SELECT payload FROM editorial_packets WHERE job=? ORDER BY stage,unit,round")?,"responses":query("SELECT json_object('stage',stage,'unit',unit,'input_hash',input_hash,'restored_markdown',markdown,'response',json(response)) FROM editorial_results WHERE job=? ORDER BY stage,unit")?,"revisions":query("SELECT payload FROM editorial_revisions WHERE job=? ORDER BY id")?,"interventions":query("SELECT payload FROM editorial_interventions WHERE job=? ORDER BY id")?,"expert_feedback":query("SELECT json_object('id',id,'segment',segment,'original_source',json(original_source),'model_text',model_text,'expert_correction',corrected_text,'reason',reason,'created',created) FROM editorial_feedback WHERE job=? ORDER BY id")?}),
    )
}
