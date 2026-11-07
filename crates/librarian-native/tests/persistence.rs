use librarian_native::{config::Config, pipeline, store::Library};
use std::sync::{Arc, atomic::AtomicBool};

// This exercises the real parser, planner, SQLite journal, publisher and recovery
// path. A long unbroken Unicode token crosses both transport and edit boundaries;
// checking the output byte-for-byte catches dropped, duplicated or inserted text.
#[tokio::test]
async fn long_unicode_survives_chunking_and_recovery_without_duplicate_publication() {
    let temp = tempfile::tempdir().unwrap();
    let source = temp.path().join("source.txt");
    let destination = temp.path().join("destination");
    let root = temp.path().join("library");
    let text = format!(
        "sentinel {} end\n\nA second paragraph.",
        "aé界🦀e\u{301}".repeat(30_000)
    );
    std::fs::write(&source, &text).unwrap();
    let mut config = Config::default();
    config.processing.edit = false;
    config.processing.classify = false;
    config.processing.vision = false;
    config.processing.chunk_bytes = 512;
    config.output.frontmatter = false;
    config.output.title = false;
    config.output.summary = false;
    config.output.review_notes = false;
    let library = Library::open(&root).unwrap();
    let id = pipeline::queue(&library, &source, &destination, &config, false, true).unwrap()[0];
    pipeline::run_queue(&library, Arc::new(AtomicBool::new(false)))
        .await
        .unwrap();
    let job = library.job(id).unwrap();
    assert_eq!(job.status, "complete", "{:?}", job.error);
    let output = job.output.unwrap();
    assert_eq!(
        std::fs::read_to_string(&output).unwrap(),
        format!("{text}\n")
    );

    // The crash window is after the file rename but before finishing the job and
    // indexing it. Source changes after ingestion must not change a resumed job.
    std::fs::write(&source, "The external source changed after ingestion.").unwrap();
    library
        .db
        .execute(
            "UPDATE jobs SET status='running',stage='exporting',output=NULL WHERE id=?",
            [id],
        )
        .unwrap();
    library
        .db
        .execute("UPDATE publications SET state='prepared' WHERE job=?", [id])
        .unwrap();
    library
        .db
        .execute("DELETE FROM search_index WHERE job=?", [id])
        .unwrap();
    drop(library);
    let library = Library::open(&root).unwrap();
    pipeline::run_queue(&library, Arc::new(AtomicBool::new(false)))
        .await
        .unwrap();
    let recovered = library.job(id).unwrap();
    assert_eq!(recovered.status, "complete", "{:?}", recovered.error);
    assert_eq!(recovered.output.as_deref(), Some(output.as_str()));
    assert_eq!(
        std::fs::read_to_string(&output).unwrap(),
        format!("{text}\n")
    );
    let publications = std::fs::read_dir(&destination)
        .unwrap()
        .filter_map(Result::ok)
        .filter(|e| e.path().extension().is_some_and(|e| e == "md"))
        .count();
    assert_eq!(
        publications, 1,
        "Recovery must reuse the committed publication"
    );
    assert!(
        !library.search("sentinel", 10).unwrap().is_empty(),
        "Recovery must restore the searchable index"
    );

    // A control action acknowledged before publication must not revive the job.
    library
        .db
        .execute("UPDATE jobs SET status='paused' WHERE id=?", [id])
        .unwrap();
    assert!(librarian_native::export::publish(&library, id, &config).is_err());
    assert_eq!(library.job(id).unwrap().status, "paused");
    assert_eq!(
        std::fs::read_to_string(&output).unwrap(),
        format!("{text}\n")
    );
}
