//! Source attribution interfaces, separate from profiler formats.

use std::path::{Path, PathBuf};
use stitch_core::{Activity, CpuSample, SourceAttribution, SourceBasis, SourceLocation};
use thiserror::Error;

#[derive(Clone, Debug)]
pub struct DeclaredLocation<'a> {
    pub target_id: &'a str,
    pub path: &'a str,
    pub line: u32,
}

#[derive(Debug, Error)]
pub enum MappingError {
    #[error("cannot open source root {path}: {source}")]
    Root {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("source path {0} is outside the configured source root")]
    OutsideRoot(PathBuf),
    #[error("duplicate source mapper registration {0}")]
    DuplicateMapper(String),
}

/// Source mappers may contribute zero or more claims for an evidence target. A claim
/// must name its basis and resolver so callers can distinguish declared
/// locations from debug-info or generated-source mappings.
pub trait SourceMapper {
    fn name(&self) -> &'static str;
    fn map(&self, target: SourceTarget<'_>) -> Result<Vec<SourceAttribution>, MappingError>;
}

#[derive(Clone, Copy)]
pub enum SourceTarget<'a> {
    Activity(&'a Activity),
    CpuSample(&'a CpuSample),
}

#[derive(Default)]
pub struct MappingRegistry {
    mappers: Vec<Box<dyn SourceMapper>>,
}

impl MappingRegistry {
    pub fn register(&mut self, mapper: impl SourceMapper + 'static) -> Result<(), MappingError> {
        if self.mappers.iter().any(|item| item.name() == mapper.name()) {
            return Err(MappingError::DuplicateMapper(mapper.name().into()));
        }
        self.mappers.push(Box::new(mapper));
        Ok(())
    }

    pub fn map(&self, target: SourceTarget<'_>) -> Result<Vec<SourceAttribution>, MappingError> {
        let mut claims = Vec::new();
        for mapper in &self.mappers {
            claims.extend(mapper.map(target)?);
        }
        Ok(claims)
    }
}

/// Resolves file and line hints explicitly emitted by instrumentation.
/// An unresolved hint returns None; it never becomes a verified attribution.
pub trait DeclaredLocationResolver {
    fn resolve(
        &self,
        hint: DeclaredLocation<'_>,
    ) -> Result<Option<SourceAttribution>, MappingError>;
}

pub struct WorkspaceLocationResolver {
    root: PathBuf,
    revision: Option<String>,
}

impl WorkspaceLocationResolver {
    pub fn new(root: impl AsRef<Path>, revision: Option<String>) -> Result<Self, MappingError> {
        let path = root.as_ref().to_owned();
        let root = path
            .canonicalize()
            .map_err(|source| MappingError::Root { path, source })?;
        Ok(Self { root, revision })
    }
}

impl DeclaredLocationResolver for WorkspaceLocationResolver {
    fn resolve(
        &self,
        hint: DeclaredLocation<'_>,
    ) -> Result<Option<SourceAttribution>, MappingError> {
        if hint.line == 0 || hint.path.is_empty() {
            return Ok(None);
        }
        let supplied = Path::new(hint.path);
        let candidate = if supplied.is_absolute() {
            supplied.to_owned()
        } else {
            self.root.join(supplied)
        };
        let canonical = match candidate.canonicalize() {
            Ok(path) => path,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(source) => {
                return Err(MappingError::Root {
                    path: candidate,
                    source,
                })
            }
        };
        let relative = canonical
            .strip_prefix(&self.root)
            .map_err(|_| MappingError::OutsideRoot(canonical.clone()))?;
        Ok(Some(SourceAttribution {
            target_id: hint.target_id.into(),
            location: SourceLocation {
                path: relative.display().to_string(),
                line: hint.line,
                column: None,
                function: None,
                build_id: None,
                revision: self.revision.clone(),
            },
            resolver: "workspace_declared_location".into(),
            basis: SourceBasis::DeclaredLocation,
            reason: "instrumentation declared this file and line; path exists in the source tree"
                .into(),
        }))
    }
}

impl SourceMapper for WorkspaceLocationResolver {
    fn name(&self) -> &'static str {
        "workspace_declared_location"
    }

    fn map(&self, target: SourceTarget<'_>) -> Result<Vec<SourceAttribution>, MappingError> {
        let SourceTarget::Activity(activity) = target else {
            return Ok(Vec::new());
        };
        let (Some(path), Some(line)) = (&activity.source_file, activity.source_line) else {
            return Ok(Vec::new());
        };
        Ok(self
            .resolve(DeclaredLocation {
                target_id: &activity.id,
                path,
                line,
            })?
            .into_iter()
            .collect())
    }
}
