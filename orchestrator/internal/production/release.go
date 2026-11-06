package production

import (
	"fmt"
	"path/filepath"
)

// A release is the folder the installer makes, with the host in its bin folder:
//
//	RELEASE/bin/vibesys-client vibesys-host vibesys-cli
//	RELEASE/sidecar/           pyproject.toml, uv.lock, src/, .venv/ (made at install by uv sync --frozen)
//	RELEASE/marketplace/       .claude-plugin/marketplace.json, skills/...
//
// The host finds the parts it starts agents with relative to its own executable, so an installed
// host needs no setting. The environment names stay as overrides for a developer's checkout, and a
// checkout is not taken for a release: a part is only accepted when the files a built release holds
// are there.

// releaseDir is the release an executable is installed in: the parent of its bin folder.
func releaseDir(executable string) (string, error) {
	if !filepath.IsAbs(executable) {
		return "", fmt.Errorf("the host's own path %q is not absolute, so its release folder is unknown", executable)
	}
	bin := filepath.Dir(executable)
	if filepath.Base(bin) != "bin" {
		return "", fmt.Errorf("%s is not in a release's bin folder", executable)
	}
	return filepath.Dir(bin), nil
}

// ReleaseSidecar is the Python of the release's sidecar environment,
// RELEASE/sidecar/.venv/bin/python, run as `python -m vs_sidecar`, which needs no uv. It is an
// error naming the path it looked at when the release has no such environment.
func ReleaseSidecar(executable string, files Files) (string, error) {
	release, err := releaseDir(executable)
	if err != nil {
		return "", fmt.Errorf("the host is not in a release (%w)", err)
	}
	python := filepath.Join(release, "sidecar", ".venv", "bin", "python")
	project := filepath.Join(release, "sidecar", "pyproject.toml")
	if !files.Executable(python) || !files.Regular(project) {
		return "", fmt.Errorf("the release has no sidecar environment at %s (with %s)", python, project)
	}
	return python, nil
}

// ReleaseMarketplace is the release's plugin marketplace, RELEASE/marketplace, when it holds
// .claude-plugin/marketplace.json. It is an error naming the path it looked at otherwise.
func ReleaseMarketplace(executable string, files Files) (string, error) {
	release, err := releaseDir(executable)
	if err != nil {
		return "", fmt.Errorf("the host is not in a release (%w)", err)
	}
	manifest := filepath.Join(release, "marketplace", ".claude-plugin", "marketplace.json")
	if !files.Regular(manifest) {
		return "", fmt.Errorf("the release has no marketplace at %s", manifest)
	}
	return filepath.Join(release, "marketplace"), nil
}
