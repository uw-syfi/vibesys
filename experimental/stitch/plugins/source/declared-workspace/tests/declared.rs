use std::fs;
use stitch_core::{
    Activity, ActivityKind, Interval, Origin, SourceAttribution, SourceBasis, SourceLocation,
};
use stitch_source_api::{
    DeclaredLocation, MappingError, MappingRegistry, SourceMapper, SourceTarget,
};
use stitch_source_workspace::{DeclaredLocationResolver, WorkspaceLocationResolver};

#[test]
fn declared_location_maps_only_to_existing_source_within_root() {
    let dir = tempfile::tempdir().unwrap();
    fs::create_dir(dir.path().join("src")).unwrap();
    fs::write(dir.path().join("src/main.rs"), "fn main() {}\n").unwrap();
    let mapper = WorkspaceLocationResolver::new(dir.path(), Some("revision".into())).unwrap();
    let resolved = mapper
        .resolve(DeclaredLocation {
            target_id: "poll",
            path: "src/main.rs",
            line: 1,
        })
        .unwrap()
        .unwrap();
    assert_eq!(resolved.location.path, "src/main.rs");
    assert_eq!(resolved.location.revision.as_deref(), Some("revision"));
    assert!(mapper
        .resolve(DeclaredLocation {
            target_id: "poll",
            path: "src/absent.rs",
            line: 1,
        })
        .unwrap()
        .is_none());
}

struct AlternateMapper;

impl SourceMapper for AlternateMapper {
    fn name(&self) -> &'static str {
        "alternate"
    }

    fn map(&self, target: SourceTarget<'_>) -> Result<Vec<SourceAttribution>, MappingError> {
        let SourceTarget::Activity(activity) = target else {
            return Ok(Vec::new());
        };
        Ok(vec![SourceAttribution {
            target_id: activity.id.clone(),
            location: SourceLocation {
                path: "generated/original.rs".into(),
                line: 9,
                column: None,
                function: None,
                build_id: None,
                revision: None,
            },
            resolver: self.name().into(),
            basis: SourceBasis::GeneratedSourceMap,
            reason: "test mapping".into(),
        }])
    }
}

#[test]
fn mapping_plugins_can_add_independent_claims() {
    let activity = Activity {
        id: "a".into(),
        kind: ActivityKind::Span,
        name: "operation".into(),
        interval: Interval {
            start_ns: 1,
            end_ns: 2,
            clock: "unix".into(),
        },
        service: None,
        trace_id: None,
        span_id: None,
        parent_span_id: None,
        pid: None,
        tid: None,
        source_file: None,
        source_line: None,
        origin: Origin {
            source: "test".into(),
            artifact: "test".into(),
            record: "1".into(),
        },
    };
    let mut registry = MappingRegistry::default();
    registry.register(AlternateMapper).unwrap();
    assert!(registry.register(AlternateMapper).is_err());
    let claims = registry.map(SourceTarget::Activity(&activity)).unwrap();
    assert_eq!(claims[0].location.path, "generated/original.rs");
}
