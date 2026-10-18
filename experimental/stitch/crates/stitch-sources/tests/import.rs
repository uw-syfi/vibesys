use std::fs;
use stitch_core::Observation;
use stitch_sources::{builtin_importers, ImportError, Importer, ImporterRegistry, InputSpec};

#[test]
fn otlp_and_perf_import_preserve_identities_and_precise_timestamps() {
    let dir = tempfile::tempdir().unwrap();
    let otlp = dir.path().join("otel.json");
    fs::write(&otlp, r#"{"resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"hotel_frontend"}}]},"scopeSpans":[{"spans":[{"traceId":"t1","spanId":"s1","name":"search","startTimeUnixNano":"100","endTimeUnixNano":"200"}]}]}]}"#).unwrap();
    let spans = builtin_importers()
        .import(&InputSpec {
            format: "otlp_json".into(),
            path: otlp,
            clock: "unix".into(),
        })
        .unwrap();
    assert!(
        matches!(&spans[0], Observation::Activity(a) if a.trace_id.as_deref() == Some("t1") && a.service.as_deref() == Some("hotel_frontend"))
    );

    let perf = dir.path().join("perf.script");
    fs::write(&perf, "7 8 12.123456789: hotel::search\n").unwrap();
    let samples = builtin_importers()
        .import(&InputSpec {
            format: "perf_script".into(),
            path: perf,
            clock: "mono".into(),
        })
        .unwrap();
    assert!(
        matches!(&samples[0], Observation::CpuSample(s) if s.at_ns == 12_123_456_789 && s.frames == ["hotel::search"])
    );
}

#[test]
fn perf_script_call_chains_from_real_export_are_imported_as_samples() {
    let dir = tempfile::tempdir().unwrap();
    let perf = dir.path().join("perf.script");
    fs::write(
        &perf,
        "2673492/2673492 1209324.226453455: \n\tffffffffaee9332b apparmor_file_permission\n\t           f8340 write\n\n2673492/2673492 1209324.228862895: \n\t           f8340 write\n",
    )
    .unwrap();
    let samples = builtin_importers()
        .import(&InputSpec {
            format: "perf_script".into(),
            path: perf,
            clock: "monotonic".into(),
        })
        .unwrap();
    assert_eq!(samples.len(), 2);
    assert!(matches!(&samples[0], Observation::CpuSample(s)
        if s.pid == 2_673_492 && s.tid == 2_673_492
        && s.at_ns == 1_209_324_226_453_455
        && s.frames == ["apparmor_file_permission", "write"]));
}

#[test]
fn perf_script_without_call_chains_is_rejected() {
    let dir = tempfile::tempdir().unwrap();
    let perf = dir.path().join("perf.script");
    fs::write(&perf, "7/8 12.123456789: \n").unwrap();
    let result = builtin_importers().import(&InputSpec {
        format: "perf_script".into(),
        path: perf,
        clock: "monotonic".into(),
    });
    assert!(matches!(result, Err(ImportError::Invalid { detail, .. })
        if detail.contains("no frames")));
}

#[test]
fn malformed_and_unknown_bridge_fields_fail_at_the_import_boundary() {
    let dir = tempfile::tempdir().unwrap();
    let bridge = dir.path().join("dial9.jsonl");
    fs::write(&bridge, r#"{"task_id":"task","poll_id":"1","name":"handler","pid":7,"tid":8,"start_ns":10,"end_ns":20,"extra":1}"#).unwrap();
    let result = builtin_importers().import(&InputSpec {
        format: "dial9_bridge_jsonl".into(),
        path: bridge,
        clock: "mono".into(),
    });
    assert!(result.is_err());
}

struct CustomImporter;

impl Importer for CustomImporter {
    fn format(&self) -> &'static str {
        "custom_profile"
    }

    fn import(&self, _input: &InputSpec) -> Result<Vec<Observation>, ImportError> {
        Ok(Vec::new())
    }
}

#[test]
fn independently_registered_importer_is_selected_by_manifest_format() {
    let mut registry = ImporterRegistry::default();
    registry.register(CustomImporter).unwrap();
    let input = InputSpec {
        format: "custom_profile".into(),
        path: "unused".into(),
        clock: "mono".into(),
    };
    assert!(registry.import(&input).unwrap().is_empty());
    assert!(registry.register(CustomImporter).is_err());
    assert!(builtin_importers().import(&input).is_err());
}
