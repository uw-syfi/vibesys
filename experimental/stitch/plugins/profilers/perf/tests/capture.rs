use std::cell::RefCell;
use std::fs;
use std::path::PathBuf;
use std::process::Output;
use stitch_core::Graph;
use stitch_importer_api::ImporterRegistry;
use stitch_profiler_perf::{
    capture_perf, CaptureError, CaptureRequest, CaptureStackMode, CommandRunner, PerfScriptImporter,
};
use stitch_session::{ingest_with, load_manifest, RunManifest, SessionError};
use stitch_source_api::MappingRegistry;

fn ingest(manifest: &RunManifest) -> Result<Graph, SessionError> {
    let mut importers = ImporterRegistry::default();
    importers.register(PerfScriptImporter)?;
    ingest_with(manifest, &importers, &MappingRegistry::default())
}

struct FakeRunner {
    calls: RefCell<Vec<Vec<String>>>,
    script: Vec<u8>,
}

impl CommandRunner for FakeRunner {
    fn run(&self, _program: &str, args: &[String]) -> Result<Output, CaptureError> {
        self.calls.borrow_mut().push(args.to_vec());
        #[cfg(unix)]
        {
            use std::os::unix::process::ExitStatusExt;
            Ok(Output {
                status: std::process::ExitStatus::from_raw(0),
                stdout: if args[0] == "script" {
                    self.script.clone()
                } else {
                    Vec::new()
                },
                stderr: Vec::new(),
            })
        }
        #[cfg(not(unix))]
        unreachable!("perf capture is Linux-only")
    }
}

#[test]
fn capture_records_a_reopenable_manifest_and_imports_samples() {
    let dir = tempfile::tempdir().unwrap();
    let output_dir = dir.path().join("capture");
    let runner = FakeRunner {
        calls: RefCell::new(Vec::new()),
        script: b"7 8 12.123456789: hotel::search\n".to_vec(),
    };
    capture_perf(
        &CaptureRequest {
            pid: 7,
            seconds: 2,
            output_dir: output_dir.clone(),
            stack_mode: CaptureStackMode::Dwarf,
        },
        &runner,
    )
    .unwrap();
    let commands = runner.calls.borrow();
    assert_eq!(commands.len(), 2);
    assert!(commands[0].contains(&"7".to_string()));
    assert!(commands[0].contains(&"dwarf,8192".to_string()));
    assert!(commands[1].contains(&"pid,tid,time,ip,sym".to_string()));
    assert!(commands[1].contains(&"--no-inline".to_string()));
    assert!(!commands[1].contains(&"-G".to_string()));
    drop(commands);
    let manifest = load_manifest(&output_dir.join("manifest.json")).unwrap();
    let graph = ingest(&manifest).unwrap();
    assert_eq!(graph.cpu_samples.len(), 1);
    assert_eq!(graph.unassigned_samples, 1);
    assert_eq!(graph.cpu_samples[0].frames, ["hotel::search"]);
}

#[test]
fn capture_accepts_frame_pointer_unwinding() {
    let dir = tempfile::tempdir().unwrap();
    let runner = FakeRunner {
        calls: RefCell::new(Vec::new()),
        script: b"7 8 12.123456789: hotel::search\n".to_vec(),
    };
    capture_perf(
        &CaptureRequest {
            pid: 7,
            seconds: 2,
            output_dir: dir.path().join("capture"),
            stack_mode: CaptureStackMode::FramePointer,
        },
        &runner,
    )
    .unwrap();
    let commands = runner.calls.borrow();
    assert!(commands[0].contains(&"fp".to_string()));
    assert!(!commands[0].contains(&"dwarf,8192".to_string()));
}

#[test]
fn capture_rejects_reusing_an_output_directory() {
    let dir = tempfile::tempdir().unwrap();
    let runner = FakeRunner {
        calls: RefCell::new(Vec::new()),
        script: Vec::new(),
    };
    let result = capture_perf(
        &CaptureRequest {
            pid: 7,
            seconds: 1,
            output_dir: PathBuf::from(dir.path()),
            stack_mode: CaptureStackMode::FramePointer,
        },
        &runner,
    );
    assert!(result.is_err());
    assert!(runner.calls.borrow().is_empty());
}

#[test]
fn empty_capture_and_empty_ingest_do_not_claim_success() {
    let dir = tempfile::tempdir().unwrap();
    let runner = FakeRunner {
        calls: RefCell::new(Vec::new()),
        script: Vec::new(),
    };
    let result = capture_perf(
        &CaptureRequest {
            pid: 7,
            seconds: 1,
            output_dir: dir.path().join("empty"),
            stack_mode: CaptureStackMode::FramePointer,
        },
        &runner,
    );
    assert!(matches!(result, Err(CaptureError::Invalid(detail))
        if detail.contains("no CPU samples")));
    let manifest = load_manifest(&dir.path().join("empty/manifest.json"));
    assert!(manifest.is_err());
    let manifest_path = dir.path().join("manifest.json");
    fs::write(
        &manifest_path,
        r#"{"schema_version":1,"label":"empty","inputs":[{"format":"perf_script","path":"empty/perf.script","clock":"monotonic"}],"source_root":null,"source_revision":null}"#,
    )
    .unwrap();
    let result = ingest(&load_manifest(&manifest_path).unwrap());
    assert!(matches!(result, Err(SessionError::Invalid(detail))
        if detail.contains("no observations")));
}
