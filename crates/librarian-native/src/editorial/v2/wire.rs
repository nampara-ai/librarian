use super::*;

fn role(stage: &str) -> (&'static str, &'static str) {
    match stage {
        "copyeditor" => (editorial::COPYEDITOR_VERSION, editorial::COPYEDITOR),
        "reviewer" => (editorial::REVIEWER_VERSION, editorial::REVIEWER),
        _ => ("typesetter-v2", editorial::TYPESETTER_V2),
    }
}

fn cited(ids: &[String], seen: &BTreeSet<String>) -> bool {
    !ids.is_empty() && ids.iter().all(|id| seen.contains(id))
}

fn validate_response(value: &Value, owned: &[String]) -> Result<()> {
    let response: contract::Response = serde_json::from_value(value.clone())?;
    if !matches!(response.status.as_str(), "complete" | "needs_evidence")
        || response.source_coverage.iter().any(|v| {
            !matches!(
                v.disposition.as_str(),
                "represented" | "merged" | "unresolved"
            )
        })
        || response
            .ledger_updates
            .iter()
            .any(|v| !matches!(v.operation.as_str(), "set" | "forget"))
    {
        bail!("Editorial contract: unsupported status, coverage disposition or ledger operation");
    }
    if response.status == "complete" {
        let expected: BTreeSet<_> = owned.iter().collect();
        let actual: BTreeSet<_> = response
            .source_coverage
            .iter()
            .map(|v| &v.source_span_id)
            .collect();
        if actual != expected || actual.len() != response.source_coverage.len() {
            bail!(
                "Editorial contract: source_coverage must account for each assigned source ID exactly once"
            );
        }
        if response
            .source_coverage
            .iter()
            .any(|v| v.reason.trim().is_empty())
        {
            bail!("Editorial contract: source coverage needs an assessment reason");
        }
        if response.edited_markdown.trim().is_empty()
            && !owned.is_empty()
            && (!response
                .source_coverage
                .iter()
                .all(|c| c.disposition == "merged")
                || !response
                    .interventions
                    .iter()
                    .any(|i| i.category == "redundancy_compression" && !i.reason.trim().is_empty()))
        {
            bail!(
                "Editorial contract: an empty passage requires an explicit, justified redundancy disposition; otherwise return the complete edited source"
            );
        }
    }
    if response.revision_requests.len() > 8
        || response.evidence_requests.len() > 8
        || response.ledger_updates.len() > 32
        || response.interventions.len() > 32
        || response.artifact_repairs.len() > 16
        || response.evidence_requests.iter().any(|r| r.length > 6000)
    {
        bail!("Editorial contract: too many operations in one response");
    }
    Ok(())
}

fn compact(packet: &mut Value) {
    for key in ["supporting_evidence", "protected_artifacts"] {
        if let Some(items) = packet[key].as_array_mut() {
            for item in items {
                for field in ["text", "read_only_representation"] {
                    if let Some(text) = item[field].as_str() {
                        let short: String = text.chars().take(300).collect();
                        if short.len() < text.len() {
                            item[field] = json!(short);
                            item["truncated"] = json!(true);
                        }
                    }
                }
            }
        }
    }
    if let Some(ledger) = packet["continuity"]["ledger"].as_array_mut() {
        ledger.truncate(8);
    }
}

fn committed(library: &Library, job: i64, unit: &Unit) -> Result<Option<String>> {
    let existing: Option<(String, String)> = library
        .db
        .query_row(
            "SELECT input_hash,markdown FROM editorial_results WHERE job=? AND stage=? AND unit=?",
            params![job, unit.stage, unit.id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()?;
    if let Some((hash, text)) = existing {
        if hash != digest(&unit.text) {
            bail!("Editorial checkpoint source changed; retry as a new run");
        }
        return Ok(Some(text));
    }
    Ok(None)
}

pub async fn edit(
    library: &Library,
    job: i64,
    config: &Config,
    client: &ModelClient,
    unit: Unit,
) -> Result<String> {
    checkpoint(library, job)?;
    let input_hash = digest(&unit.text);
    if let Some(text) = committed(library, job, &unit)? {
        return Ok(text);
    }
    for id in &unit.owned {
        library.db.execute("INSERT OR IGNORE INTO editorial_owned(job,stage,unit,source_id,is_primary,status) SELECT ?,?,?,?,CASE WHEN kind LIKE '%_window' OR kind IN ('typeset_native','typeset_visual') THEN 1 ELSE 0 END,'planned' FROM editorial_sources WHERE job=? AND id=?",params![job,unit.stage,unit.id,id,job,id])?;
    }
    let (version, instructions) = role(unit.stage);
    let instructions = format!("{instructions}\n\n{}", contract::PROTOCOL);
    let actual_page = unit.page.min(counts(library, job)?.0 as i64);
    let mut protected = protection::protect(&unit.text);
    let mut previews = Vec::new();
    let mut preview_budget = 6000usize;
    let parent = unit.owned.first().cloned().unwrap_or_default();
    for artifact in &mut protected.artifacts {
        artifact.artifact_id = evidence::source(
            library,
            job,
            actual_page,
            "artifact",
            &parent,
            artifact.start,
            &artifact.content,
        )?;
        let (_, preview) = evidence::excerpt(&artifact.content, 0, preview_budget.min(1800));
        preview_budget = preview_budget.saturating_sub(preview.len());
        previews.push(json!({"artifact_id":artifact.artifact_id,"marker":artifact.marker,"kind":artifact.kind,"sha256":artifact.sha256,"read_only_representation":preview,"total_bytes":artifact.content.len(),"truncated":preview.len()<artifact.content.len()}));
    }
    let mut images = vec![evidence::page_id(library, job, actual_page)?];
    if unit.stage == "reviewer" && actual_page > 1 {
        images.push(evidence::page_id(library, job, actual_page - 1)?);
    }
    let mut continuity = unit.context.clone();
    continuity["ledger"] = json!(evidence::ledger(library, job, unit.stage, &unit.text)?);
    if unit.stage == "reviewer" {
        continuity["preceding_committed"] = json!(evidence::prior(library, job, unit.page)?);
    }
    let mut packet = json!({"role_version":version,"document_brief":evidence::brief(library,job,config)?,"editable_passage":{"segment_id":format!("{}:{}",unit.stage,unit.id),"text":protected.text,"source_span_ids":unit.owned,"document_position":if unit.page==1{"beginning"}else if extraction_done(library,job)?&&unit.page>=counts(library,job)?.0 as i64{"end"}else{"middle"},"open_structures":audit::inspect(&unit.text)},"owned_source_span_ids":unit.owned,"continuity":continuity,"supporting_evidence":unit.evidence,"protected_artifacts":previews,"available_page_images":images,"available_prior_segments":{"prefix":"review:","first":1,"last":unit.page-1},"capabilities":{"can_request_evidence":config.processing.evidence_rounds>0,"can_request_page_image":config.processing.vision&&unit.stage!="copyeditor","can_request_prior_revision":unit.stage=="reviewer","can_request_artifact_repair":true},"attached_image_id":unit.image_id});
    let mut seen: BTreeSet<String> = unit.owned.iter().cloned().collect();
    for e in &unit.evidence {
        if let Some(id) = e["source_span_id"].as_str() {
            seen.insert(id.into());
        }
    }
    for a in &protected.artifacts {
        seen.insert(a.artifact_id.clone());
    }
    let mut image = unit.image.clone();
    if image.is_some()
        && let Some(id) = &unit.image_id
    {
        seen.insert(id.clone());
    }
    let mut evidence_rounds = 0;
    let mut retrieval_bytes = 18000usize;
    let mut corrections = 0;
    let mut compacted = false;
    loop {
        checkpoint(library, job)?;
        let round = evidence_rounds + corrections + usize::from(compacted);
        packet["capabilities"]["can_request_evidence"] =
            json!(evidence_rounds < config.processing.evidence_rounds);
        // Store the actual bounded request, its role identity, and image ID. Never
        // store credentials or base64 payloads in these diagnostic records.
        library.db.execute("INSERT OR REPLACE INTO editorial_packets(job,stage,unit,round,payload) VALUES(?,?,?,?,?)",params![job,unit.stage,unit.id,round as i64,serde_json::to_string(&packet)?])?;
        let response = client
            .request(
                library,
                job,
                Request {
                    task: &format!("editorial_{}_v2", unit.stage),
                    instructions: &instructions,
                    context: None,
                    input: packet.clone(),
                    schema: contract::schema(),
                    image: image.as_ref().map(|p| (p.as_slice(), "image/png")),
                    validate: &|v| validate_response(v, &unit.owned),
                },
            )
            .await;
        let mut response: contract::Response = match response {
            Ok(value) => serde_json::from_value(value)?,
            Err(error) if error.to_string().contains("context budget") && !compacted => {
                compact(&mut packet);
                compacted = true;
                continue;
            }
            Err(error)
                if !model_limit(&error)
                    && corrections < 2
                    && (error.to_string().contains("Editorial contract")
                        || error.to_string().contains("Provider JSON")
                        || error.to_string().contains("missing field")) =>
            {
                packet["response_correction"] = json!(format!(
                    "Return a complete response conforming to the schema: {error}"
                ));
                corrections += 1;
                continue;
            }
            Err(error) => return Err(error),
        };
        if response.status == "needs_evidence"
            && evidence_rounds < config.processing.evidence_rounds
        {
            let mut fetched = Vec::new();
            let mut attached = false;
            for request in &response.evidence_requests {
                if request.kind == "page_image" {
                    if config.processing.vision && unit.stage != "copyeditor" && !attached {
                        match evidence::image_path(library,job,&request.id).and_then(|path|model_image(Path::new(&path),config.processing.render_width)) {
                            Ok(page)=>{let mut bytes=std::io::Cursor::new(Vec::new());page.write_to(&mut bytes,image::ImageFormat::Png)?;image=Some(bytes.into_inner());packet["attached_image_id"]=json!(request.id);seen.insert(request.id.clone());attached=true;fetched.push(json!({"id":request.id,"available":true,"attached_image":true}));}
                            Err(error)=>fetched.push(json!({"id":request.id,"available":false,"reason":error.to_string()})),
                        }
                    } else {
                        fetched.push(json!({"id":request.id,"available":false,"reason":"At most one image per request; image inspection is disabled for the native Copyeditor."}));
                    }
                } else {
                    if retrieval_bytes == 0 {
                        fetched.push(json!({"id":request.id,"available":false,"reason":"The bounded source retrieval allowance is exhausted; preserve the strongest supportable reading and record remaining uncertainty."}));
                        continue;
                    }
                    let mut bounded = request.clone();
                    bounded.length = request.length.clamp(1, 6000).min(retrieval_bytes);
                    match evidence::fetch(library, job, unit.page, &bounded) {
                        Ok(value) => {
                            retrieval_bytes = retrieval_bytes
                                .saturating_sub(value["text"].as_str().map_or(0, str::len));
                            seen.insert(request.id.clone());
                            fetched.push(value);
                        }
                        Err(error) => fetched.push(
                            json!({"id":request.id,"available":false,"reason":error.to_string()}),
                        ),
                    }
                }
            }
            // Keep retrieved evidence available through this bounded dialogue.
            // Otherwise a later request would erase evidence used by a revision.
            let history = packet
                .as_object_mut()
                .unwrap()
                .entry("requested_evidence")
                .or_insert_with(|| json!([]));
            history.as_array_mut().unwrap().extend(fetched);
            packet["previous_draft"] = json!(response.edited_markdown);
            packet["evidence_instruction"] = json!(
                "Use the retrieved evidence to finish. Unavailable or incomplete evidence must remain an explicit internal finding; do not invent missing facts."
            );
            evidence_rounds += 1;
            continue;
        }
        if response.status == "needs_evidence" {
            if response.edited_markdown.trim().is_empty() && corrections < 2 {
                packet["response_correction"] = json!(
                    "No more evidence retrieval rounds are available. Return status complete with the strongest supportable edited passage and internal unresolved_findings."
                );
                corrections += 1;
                continue;
            }
            response.unresolved_findings.push(contract::Finding{kind:"evidence_budget".into(),description:"Additional evidence could not be obtained within this unit's retrieval allowance.".into(),evidence_ids:response.evidence_requests.iter().map(|r|r.id.clone()).collect()});
            response.status = "complete".into();
            validate_response(&serde_json::to_value(&response)?, &unit.owned)?;
        }
        let prepared = prepare(library, job, &unit, &protected.artifacts, &seen, &response);
        let (markdown, revisions) = match prepared {
            Ok(value) => value,
            Err(error) if corrections < 2 => {
                packet["response_correction"] = json!(format!(
                    "The proposed operations could not be safely applied: {error}. Keep all artifact markers intact; fetch current prior passages if needed, or omit the unsupported revision and record an unresolved finding. Return the complete edited passage again."
                ));
                packet["previous_draft"] = json!(response.edited_markdown);
                if unit.stage == "reviewer" {
                    packet["continuity"]["preceding_committed"] =
                        json!(evidence::prior(library, job, unit.page)?);
                }
                corrections += 1;
                continue;
            }
            Err(error) => {
                // A stale/unsupported proposed revision must not strand otherwise
                // usable prose. Retain prior text and exact artifacts, and expose
                // the unapplied operation in internal diagnostics.
                response.unresolved_findings.push(contract::Finding {
                    kind: "unapplied_revision".into(),
                    description: format!(
                        "Requested operation could not be applied after bounded correction: {error}"
                    ),
                    evidence_ids: vec![],
                });
                response.revision_requests.clear();
                response.artifact_repairs.clear();
                prepare(library,job,&unit,&protected.artifacts,&seen,&response).context("The response still omitted or duplicated protected content; actual unprocessed source and completed work retained")?
            }
        };
        let findings = audit::inspect(&markdown);
        if corrections < 2
            && findings.iter().any(|f| f.kind == "structured_syntax")
            && audit::inspect(&unit.text)
                .iter()
                .all(|f| f.kind != "structured_syntax")
        {
            packet["response_correction"] = json!({"instruction":"The new output introduced structured syntax errors. Repair the new structure while preserving all source data. Exact original protected artifacts remain read-only unless separately repaired with evidence.","findings":findings});
            packet["previous_draft"] = json!(response.edited_markdown);
            corrections += 1;
            continue;
        }
        response.unresolved_findings.extend(findings);
        commit(
            library,
            job,
            &unit,
            &input_hash,
            &markdown,
            &response,
            &revisions,
        )?;
        return Ok(markdown);
    }
}

struct PreparedRevision {
    unit: String,
    before: String,
    after: String,
    request: contract::Revision,
}

fn prepare(
    library: &Library,
    job: i64,
    unit: &Unit,
    artifacts: &[Artifact],
    seen: &BTreeSet<String>,
    response: &contract::Response,
) -> Result<(String, Vec<PreparedRevision>)> {
    let mut artifacts = artifacts.to_vec();
    let mut repairs = BTreeMap::new();
    for repair in &response.artifact_repairs {
        let mut original_evidence = false;
        for id in &repair.evidence_ids {
            original_evidence |= library.db.query_row("SELECT EXISTS(SELECT 1 FROM editorial_sources WHERE job=? AND id=? AND kind IN ('native','ocr','visual','ocr_geometry','page_image'))",params![job,id],|r|r.get::<_,bool>(0))?;
        }
        if repair.reason.trim().is_empty()
            || !cited(&repair.evidence_ids, seen)
            || !original_evidence
            || repair
                .evidence_ids
                .iter()
                .all(|id| id == &repair.artifact_id)
        {
            bail!(
                "Artifact repair requires inspected original native/OCR/visual/image evidence and a reason"
            );
        }
        let (hash, content): (String, String) = library.db.query_row(
            "SELECT sha256,content FROM editorial_sources WHERE job=? AND id=? AND kind='artifact'",
            params![job, repair.artifact_id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        if hash != repair.expected_hash {
            bail!("Artifact repair hash is stale");
        }
        if audit::inspect(&repair.replacement).iter().any(|f| {
            matches!(
                f.kind.as_str(),
                "structured_syntax" | "technical_delimiters" | "math_delimiters" | "markdown_fence"
            )
        }) {
            bail!("Artifact repair still has invalid structure");
        }
        if repairs
            .insert(
                repair.artifact_id.clone(),
                (content, repair.replacement.clone()),
            )
            .is_some()
        {
            bail!("Duplicate artifact repair");
        }
    }
    let mut used = BTreeSet::new();
    for artifact in &mut artifacts {
        if let Some((_, replacement)) = repairs.get(&artifact.artifact_id) {
            artifact.content = replacement.clone();
            artifact.sha256 = digest(replacement);
            used.insert(artifact.artifact_id.clone());
        }
    }
    let markdown =
        protection::restore(&response.edited_markdown, &artifacts).map_err(anyhow::Error::msg)?;
    let mut revisions = Vec::new();
    let mut targets = BTreeSet::new();
    for revision in &response.revision_requests {
        if unit.stage != "reviewer" {
            bail!("Only the Reviewer can revise prior committed passages");
        }
        if !cited(&revision.evidence_ids, seen) || revision.reason.trim().is_empty() {
            bail!("Prior revision must cite inspected evidence and explain its purpose");
        }
        let target = revision
            .target_segment_id
            .strip_prefix("review:")
            .and_then(|s| s.parse::<i64>().ok())
            .filter(|n| *n > 0 && *n < unit.page)
            .context("Revision must target an earlier review:N segment")?;
        if !targets.insert(target) {
            bail!("Combine changes to the same prior segment into one revision");
        }
        let before = saved::<Handoff>(library, job, "review", &target.to_string())?
            .context("Prior review segment is unavailable")?;
        let next = contract::revised(&before.emitted, revision).map_err(anyhow::Error::msg)?;
        let mut approved_before = before.emitted.clone();
        for (id, (original, replacement)) in &repairs {
            if approved_before.contains(original) && revision.old_text.contains(original) {
                approved_before = approved_before.replacen(original, replacement, 1);
                used.insert(id.clone());
            }
        }
        if !protection::literals_preserved(&approved_before, &next) {
            bail!("Prior revision changes protected literals without a separate artifact repair");
        }
        revisions.push(PreparedRevision {
            unit: target.to_string(),
            before: before.emitted,
            after: next,
            request: revision.clone(),
        });
    }
    if used.len() != repairs.len() {
        bail!(
            "Artifact repair is not connected to the editable passage or a requested prior revision"
        );
    }
    Ok((markdown, revisions))
}

fn commit(
    library: &Library,
    job: i64,
    unit: &Unit,
    input_hash: &str,
    markdown: &str,
    response: &contract::Response,
    revisions: &[PreparedRevision],
) -> Result<()> {
    let tx = rusqlite::Transaction::new_unchecked(
        &library.db,
        rusqlite::TransactionBehavior::Immediate,
    )?;
    checkpoint(library, job)?;
    // Validate the entire set under the write lock before changing any passage.
    for revision in revisions {
        let current = saved::<Handoff>(library, job, "review", &revision.unit)?
            .context("Prior segment disappeared")?;
        if current.emitted != revision.before {
            bail!("Prior segment changed before commit; no revisions were applied");
        }
    }
    for revision in revisions {
        let mut current = saved::<Handoff>(library, job, "review", &revision.unit)?.unwrap();
        current.emitted = revision.after.clone();
        current.context = editorial::tail(&current.emitted, 1200);
        save(library, job, "review", &revision.unit, &current)?;
        let payload = json!({"request":revision.request,"before":revision.before,"after":revision.after,"status":"applied"});
        library.db.execute("INSERT INTO editorial_revisions(job,unit,target,before_hash,after_hash,payload) VALUES(?,?,?,?,?,?)",params![job,unit.id,revision.unit,digest(&revision.before),digest(&revision.after),serde_json::to_string(&payload)?])?;
        // The model's updates below establish corrected facts. Entries grounded
        // solely in a changed segment must not silently survive as authoritative.
        library.db.execute(
            "DELETE FROM editorial_ledger WHERE job=? AND stage='reviewer' AND unit LIKE ?",
            params![job, format!("{}:%", revision.unit)],
        )?;
    }
    let categories = [
        "subject",
        "purpose",
        "language",
        "speakers",
        "names",
        "terminology",
        "abbreviations",
        "units",
        "section",
        "open_structures",
        "unresolved_references",
        "decisions",
    ];
    for update in &response.ledger_updates {
        if !categories.contains(&update.category.as_str())
            || update.key.len() > 256
            || update.value.len() > 2048
        {
            continue;
        }
        if update.operation == "forget" {
            // A tombstone also suppresses an obsolete upstream role's value.
            library.db.execute(
                "INSERT OR REPLACE INTO editorial_ledger(job,stage,category,key,value,unit,updated) VALUES(?,?,?,?,'',?,?)",
                params![job, unit.stage, update.category, update.key,unit.id,crate::store::now()],
            )?;
        } else {
            library.db.execute("INSERT OR REPLACE INTO editorial_ledger(job,stage,category,key,value,unit,updated) VALUES(?,?,?,?,?,?,?)",params![job,unit.stage,update.category,update.key,update.value,unit.id,crate::store::now()])?;
        }
    }
    for coverage in &response.source_coverage {
        library.db.execute("INSERT INTO editorial_coverage(job,stage,unit,source_id,disposition,reason) VALUES(?,?,?,?,?,?)",params![job,unit.stage,unit.id,coverage.source_span_id,coverage.disposition,coverage.reason])?;
    }
    for intervention in &response.interventions {
        let payload = json!({"assessment":"model","record":intervention,"before_in_editable_source":unit.text.contains(&intervention.before),"after_in_edited_output":markdown.contains(&intervention.after)});
        library.db.execute(
            "INSERT INTO editorial_interventions(job,stage,unit,payload) VALUES(?,?,?,?)",
            params![job, unit.stage, unit.id, serde_json::to_string(&payload)?],
        )?;
    }
    library.db.execute("INSERT INTO editorial_results(job,stage,unit,input_hash,markdown,response) VALUES(?,?,?,?,?,?)",params![job,unit.stage,unit.id,input_hash,markdown,serde_json::to_string(response)?])?;
    library.db.execute(
        "UPDATE editorial_owned SET status='complete' WHERE job=? AND stage=? AND unit=?",
        params![job, unit.stage, unit.id],
    )?;
    tx.commit()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    // Real SQLite transaction + reopening, with a concurrent stale target and
    // pause injected at the write boundary. No provider or mocked persistence.
    #[test]
    fn retrospective_edits_are_atomic_pause_safe_and_resume_without_replay() -> Result<()> {
        let temp = tempfile::tempdir()?;
        let library = Library::open(temp.path())?;
        library.db.execute("INSERT INTO jobs(id,source,destination,config,status,created,updated) VALUES(1,'source','destination','{}','running',0,0)",[])?;
        let first = "Speaker: Jon. Use `--count=0012`.\n\n";
        let second = "Jon describes the result.\n\n";
        for (id, text) in [("1", first), ("2", second)] {
            save(
                &library,
                1,
                "review",
                id,
                &Handoff {
                    emitted: text.into(),
                    pending: "unpublished continuation".into(),
                    context: text.into(),
                },
            )?;
        }
        let source = evidence::source(
            &library,
            1,
            3,
            "reviewer_window",
            "origin",
            0,
            "John confirms his name.",
        )?;
        let unit = Unit {
            stage: "reviewer",
            id: "3:0:24".into(),
            page: 3,
            text: "John confirms his name.".into(),
            context: json!({}),
            owned: vec![source.clone()],
            evidence: vec![],
            image: None,
            image_id: None,
        };
        library.db.execute(
            "INSERT INTO editorial_owned VALUES(1,'reviewer',?,?,1,'planned')",
            params![unit.id, source],
        )?;
        let make_revision = |id: &str, text: &str| contract::Revision {
            target_segment_id: format!("review:{id}"),
            expected_hash: digest(text),
            old_text: "Jon".into(),
            replacement: "John".into(),
            reason: "The source explicitly identifies John.".into(),
            evidence_ids: vec![source.clone()],
        };
        let mut response = contract::Response {
            status: "complete".into(),
            edited_markdown: unit.text.clone(),
            source_coverage: vec![contract::Coverage {
                source_span_id: source.clone(),
                disposition: "represented".into(),
                reason: "Name confirmation preserved.".into(),
            }],
            revision_requests: vec![make_revision("1", first), make_revision("2", second)],
            evidence_requests: vec![],
            unresolved_findings: vec![],
            ledger_updates: vec![contract::LedgerUpdate {
                category: "names".into(),
                key: "John".into(),
                value: "Speaker explicitly identified in the source.".into(),
                operation: "set".into(),
            }],
            interventions: vec![],
            artifact_repairs: vec![],
        };
        let seen = BTreeSet::from([source]);
        validate_response(&serde_json::to_value(&response)?, &unit.owned)?;
        let (markdown, revisions) = prepare(&library, 1, &unit, &[], &seen, &response)?;
        let stale = Handoff {
            emitted: "Another revision arrived.".into(),
            ..Default::default()
        };
        save(&library, 1, "review", "2", &stale)?;
        assert!(
            commit(
                &library,
                1,
                &unit,
                &digest(&unit.text),
                &markdown,
                &response,
                &revisions
            )
            .is_err()
        );
        assert_eq!(
            saved::<Handoff>(&library, 1, "review", "1")?
                .unwrap()
                .emitted,
            first
        );
        assert!(committed(&library, 1, &unit)?.is_none());
        assert_eq!(
            library
                .db
                .query_row("SELECT count(*) FROM editorial_revisions", [], |r| r
                    .get::<_, i64>(0))?,
            0
        );
        save(
            &library,
            1,
            "review",
            "2",
            &Handoff {
                emitted: second.into(),
                ..Default::default()
            },
        )?;
        let good = response.revision_requests[0].clone();
        let mut overlapping = good.clone();
        overlapping.expected_hash = digest("111");
        overlapping.old_text = "11".into();
        assert!(contract::revised("111", &overlapping).is_err());
        response.revision_requests[0].old_text = "`--count=0012`".into();
        response.revision_requests[0].replacement = "`--count=12`".into();
        assert!(prepare(&library, 1, &unit, &[], &seen, &response).is_err());
        response.revision_requests[0] = good;
        library
            .db
            .execute("UPDATE jobs SET status='paused' WHERE id=1", [])?;
        assert!(
            commit(
                &library,
                1,
                &unit,
                &digest(&unit.text),
                &markdown,
                &response,
                &revisions
            )
            .is_err()
        );
        assert!(committed(&library, 1, &unit)?.is_none());
        library
            .db
            .execute("UPDATE jobs SET status='running' WHERE id=1", [])?;
        commit(
            &library,
            1,
            &unit,
            &digest(&unit.text),
            &markdown,
            &response,
            &revisions,
        )?;
        drop(library);
        let library = Library::open(temp.path())?;
        // Resume takes this result before dispatching a model or applying any mutation.
        assert_eq!(committed(&library, 1, &unit)?, Some(markdown));
        assert_eq!(
            library
                .db
                .query_row("SELECT count(*) FROM editorial_revisions", [], |r| r
                    .get::<_, i64>(0))?,
            2
        );
        let h = saved::<Handoff>(&library, 1, "review", "1")?.unwrap();
        assert_eq!(h.emitted, first.replace("Jon", "John"));
        assert_eq!(h.pending, "unpublished continuation");
        assert_eq!(
            library
                .db
                .query_row("SELECT status FROM editorial_owned", [], |r| r
                    .get::<_, String>(0))?,
            "complete"
        );
        assert_eq!(
            library.db.query_row(
                "SELECT value FROM editorial_ledger WHERE key='John'",
                [],
                |r| r.get::<_, String>(0)
            )?,
            "Speaker explicitly identified in the source."
        );
        // The publication path must retain reviewed bytes and boundary spacing,
        // including indented structured content, after the retrospective edits.
        let tail = format!(
            "{}\n\n    e\u{301} = −0.00120  \n    id = 0012\n\n",
            unit.text
        );
        save(
            &library,
            1,
            "review",
            "3",
            &Handoff {
                emitted: tail.clone(),
                ..Default::default()
            },
        )?;
        library.db.execute(
            "INSERT INTO editorial_runs(job,version) VALUES(1,'editorial-v2')",
            [],
        )?;
        let metadata = Metadata {
            title: "Transaction fixture".into(),
            summary: String::new(),
            tags: vec![],
            class_code: "000".into(),
            taxonomy: "local-dewey-style".into(),
        };
        library.db.execute("INSERT INTO documents(id,digest,original,format,metadata,extraction_key,created) VALUES('0123456789abcdef','digest','source','markdown',?,'fixture',0)",[serde_json::to_string(&metadata)?])?;
        library.db.execute(
            "UPDATE jobs SET document='0123456789abcdef',destination=? WHERE id=1",
            [temp.path().to_string_lossy().as_ref()],
        )?;
        crate::editorial::materialize_review(&library, 1, 2)?;
        let mut config = Config::default();
        config.output.frontmatter = false;
        config.output.title = false;
        config.output.summary = false;
        config.output.review_notes = false;
        config.output.page_markers = false;
        let output = crate::export::publish(&library, 1, &config)?;
        assert_eq!(
            std::fs::read_to_string(output)?,
            format!(
                "{}{}{tail}\n",
                first.replace("Jon", "John"),
                second.replace("Jon", "John")
            )
        );
        let mut changed = unit;
        changed.text.push_str(" A different source.");
        assert!(committed(&library, 1, &changed).is_err());
        Ok(())
    }
}
