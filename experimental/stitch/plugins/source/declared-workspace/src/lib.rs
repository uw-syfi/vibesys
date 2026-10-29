//! Validates source file and line hints declared by instrumentation.
#![forbid(unsafe_code)]
use std::path::{Path, PathBuf};
use stitch_core::{SourceAttribution, SourceBasis, SourceLocation};
use stitch_source_api::{DeclaredLocation, MappingError, SourceMapper, SourceTarget};

/// Resolves file and line hints explicitly emitted by instrumentation.
/// An unresolved hint returns None; it never becomes a verified attribution.
pub trait DeclaredLocationResolver {
    /// Resolve a declared source hint within the configured source root.
    ///
    /// # Errors
    ///
    /// Returns an error if canonicalization fails or the file escapes the root.
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
    /// Open a source root for declared location checks.
    ///
    /// # Errors
    ///
    /// Returns an error if the root cannot be canonicalized.
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
