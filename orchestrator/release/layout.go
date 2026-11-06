package release

import (
	"io/fs"
	"path/filepath"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// The layout of a release, as the builder lays it out. This is the only place that writes it down:
// everything else reads the manifest NewManifest makes.
//
//	RELEASE/release.json
//	RELEASE/bin/                 vibesys-client vibesys-host vibesys-cli
//	RELEASE/sidecar/             pyproject.toml uv.lock src/ and, once installed, .venv/
//	RELEASE/marketplace/         .claude-plugin/marketplace.json and the skills it names
const (
	clientProgram = "bin/vibesys-client"
	hostProgram   = "bin/vibesys-host"
	jobTool       = "bin/vibesys-cli"
	sidecarPython = "sidecar/.venv/bin/python"
	skillsRoot    = "marketplace"
)

// NewManifest is the manifest of a release built for goos/goarch at version.
func NewManifest(version, goos, goarch string) (*pb.ReleaseManifest, error) {
	m := &pb.ReleaseManifest{
		Version:              version,
		Os:                   goos,
		Arch:                 goarch,
		HostProgram:          hostProgram,
		SessionHolderCommand: []string{sidecarPython, "-m", "vs_sidecar"},
		Skills:               &pb.SkillBundle{Root: skillsRoot},
		JobTool:              jobTool,
		RequiredFiles: []string{
			clientProgram, hostProgram, jobTool,
			"sidecar/pyproject.toml", "sidecar/uv.lock", "sidecar/src/vs_sidecar/__init__.py",
			skillsRoot + "/.claude-plugin/marketplace.json",
		},
	}
	return m, validate(m)
}

// New is the release in dir (absolute) that m describes, for a builder to check before it writes
// the manifest.
func New(dir string, m *pb.ReleaseManifest, fsys fs.FS) *Release {
	return &Release{Dir: filepath.Clean(dir), Manifest: m, fsys: fsys}
}
