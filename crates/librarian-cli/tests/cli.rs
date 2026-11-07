use std::{fs, process::Command};

fn cli() -> Command {
    Command::new(env!("CARGO_BIN_EXE_librarian"))
}

#[test]
fn conversion_preserves_source_and_refuses_to_overwrite_output() {
    let temp = tempfile::tempdir().unwrap();
    let source = temp.path().join("source.txt");
    let output = temp.path().join("result.md");
    fs::write(&source, "A normal sentence.\n\n# literal heading").unwrap();
    let first = cli()
        .arg("convert")
        .arg(&source)
        .arg("-o")
        .arg(&output)
        .output()
        .unwrap();
    assert!(
        first.status.success(),
        "{}",
        String::from_utf8_lossy(&first.stderr)
    );
    assert_eq!(
        fs::read_to_string(&output).unwrap(),
        "A normal sentence.\n\n\\# literal heading\n"
    );
    fs::write(&output, "User edits").unwrap();
    assert!(
        !cli()
            .arg("convert")
            .arg(&source)
            .arg("-o")
            .arg(&output)
            .status()
            .unwrap()
            .success()
    );
    assert_eq!(fs::read_to_string(&output).unwrap(), "User edits");
    assert_eq!(
        fs::read_to_string(&source).unwrap(),
        "A normal sentence.\n\n# literal heading"
    );
    assert_eq!(fs::read_dir(temp.path()).unwrap().count(), 2);
}

#[test]
fn unsupported_input_and_non_markdown_output_fail_without_creating_files() {
    let temp = tempfile::tempdir().unwrap();
    let source = temp.path().join("source.txt");
    let output = temp.path().join("result.txt");
    fs::write(&source, "hello").unwrap();
    assert!(
        !cli()
            .arg("convert")
            .arg(&source)
            .arg("-o")
            .arg(&output)
            .status()
            .unwrap()
            .success()
    );
    assert!(!output.exists());
    assert!(
        !cli()
            .arg("convert")
            .arg(temp.path().join("scan.pdf"))
            .status()
            .unwrap()
            .success()
    );
}

#[test]
fn existing_markdown_can_be_piped_without_editorial_changes() {
    let temp = tempfile::tempdir().unwrap();
    let source = temp.path().join("source.md");
    let content = "# Keep\n\n```mermaid\nflowchart LR\nA --> B\n```\n";
    fs::write(&source, content).unwrap();
    let result = cli().arg("convert").arg(&source).output().unwrap();
    assert!(result.status.success());
    assert_eq!(String::from_utf8(result.stdout).unwrap(), content);
    let inspection = cli().arg("inspect").arg(&source).output().unwrap();
    assert!(inspection.status.success());
    let stats: serde_json::Value = serde_json::from_slice(&inspection.stdout).unwrap();
    assert_eq!(stats["mermaid_blocks"], 1);
}
