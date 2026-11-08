// Package release is the one definition of what an installed VibeSys release holds. The builder
// writes it as RELEASE/release.json, a ReleaseManifest; the host, the client and the installer
// (through `vibesys-client release verify`) read it, so no other program carries a list of the
// release's paths.
//
// The package reads files only through an fs.FS handed to it, so its tests lay a release out in
// memory and the programs hand it the real file system rooted at "/".
package release

import (
	"errors"
	"fmt"
	"io/fs"
	"path"
	"path/filepath"
	"slices"
	"strings"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// ManifestName is the manifest's file name, in the release folder.
const ManifestName = "release.json"

// Release is one release folder and the manifest it holds.
type Release struct {
	// Dir is the release folder, absolute.
	Dir      string
	Manifest *pb.ReleaseManifest
	fsys     fs.FS
}

// Finder looks for the release a program is installed in. Parts are resolved through it at the
// moment they are needed, so repairing an install takes effect without a restart.
type Finder func() (*Release, error)

// NotInstalledError says a program stands in no release: no manifest is beside its folder's parent.
// A developer's build is such a program, and its caller may take that as "no release".
type NotInstalledError struct {
	Program  string
	Manifest string
	Err      error
}

func (e *NotInstalledError) Error() string {
	return fmt.Sprintf("%s is not in a release: there is no %s at %s (%v)", e.Program, ManifestName, e.Manifest, e.Err)
}

func (e *NotInstalledError) Unwrap() error { return e.Err }

// Find is the release a program is installed in: the manifest sits in the folder that holds the
// program's own folder (RELEASE/release.json beside RELEASE/bin/PROGRAM). fsys is the file system
// rooted at "/" and program is an absolute path with symlinks already followed.
func Find(program string, fsys fs.FS) (*Release, error) {
	if !filepath.IsAbs(program) {
		return nil, fmt.Errorf("the program's own path %q is not absolute, so its release folder is unknown", program)
	}
	return open(filepath.Dir(filepath.Dir(program)), fsys, program)
}

// Open reads the release in dir (absolute) from fsys, which is rooted at "/".
func Open(dir string, fsys fs.FS) (*Release, error) {
	if !filepath.IsAbs(dir) {
		return nil, fmt.Errorf("the release folder %q is not absolute", dir)
	}
	return open(dir, fsys, dir)
}

func open(dir string, fsys fs.FS, program string) (*Release, error) {
	manifestPath := filepath.Join(dir, ManifestName)
	data, err := fs.ReadFile(fsys, rooted(manifestPath))
	if err != nil {
		return nil, &NotInstalledError{Program: program, Manifest: manifestPath, Err: err}
	}
	m, err := Parse(data)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", manifestPath, err)
	}
	return &Release{Dir: dir, Manifest: m, fsys: fsys}, nil
}

// Parse reads a manifest, refusing a field it does not know and any value its rules reject.
func Parse(data []byte) (*pb.ReleaseManifest, error) {
	var m pb.ReleaseManifest
	if err := protojson.Unmarshal(data, &m); err != nil {
		return nil, fmt.Errorf("not a release manifest: %w", err)
	}
	if err := validate(&m); err != nil {
		return nil, err
	}
	return &m, nil
}

// Marshal writes a manifest as the file the release holds, after checking it.
func Marshal(m *pb.ReleaseManifest) ([]byte, error) {
	if err := validate(m); err != nil {
		return nil, err
	}
	data, err := protojson.MarshalOptions{Multiline: true, Indent: "  "}.Marshal(m)
	if err != nil {
		return nil, err
	}
	return append(data, '\n'), nil
}

func validate(m *pb.ReleaseManifest) error {
	if err := protovalidate.Validate(m); err != nil {
		return fmt.Errorf("not a valid release manifest: %w", err)
	}
	return nil
}

// abs is the absolute path of a path the manifest names.
func (r *Release) abs(rel string) string { return filepath.Join(r.Dir, filepath.FromSlash(rel)) }

// Version is the release's version.
func (r *Release) Version() string { return r.Manifest.GetVersion() }

// HostProgram is the program that serves the host API. It is an *IncompleteError naming the file
// when it is missing.
func (r *Release) HostProgram() (string, error) {
	if err := r.checkExecutable(r.Manifest.GetHostProgram()); err != nil {
		return "", err
	}
	return r.abs(r.Manifest.GetHostProgram()), nil
}

// SessionHolderCommand is the command that holds an agent session: an executable of the release,
// then the words the manifest gives it. It is an *IncompleteError naming the file when the
// executable is missing.
func (r *Release) SessionHolderCommand() ([]string, error) {
	words := r.Manifest.GetSessionHolderCommand()
	if err := r.checkExecutable(words[0]); err != nil {
		return nil, err
	}
	return append([]string{r.abs(words[0])}, words[1:]...), nil
}

// SkillsRoot is the folder of skills a session loads. It is an *IncompleteError naming it when it
// is missing.
func (r *Release) SkillsRoot() (string, error) {
	root := r.Manifest.GetSkills().GetRoot()
	if info, err := fs.Stat(r.fsys, rooted(r.abs(root))); err != nil || !info.IsDir() {
		return "", &IncompleteError{Dir: r.Dir, Missing: []string{root}}
	}
	return r.abs(root), nil
}

// JobTool is the tool the skills run. It is an *IncompleteError naming the file when it is missing.
func (r *Release) JobTool() (string, error) {
	if err := r.checkExecutable(r.Manifest.GetJobTool()); err != nil {
		return "", err
	}
	return r.abs(r.Manifest.GetJobTool()), nil
}

// Stage is how much of a release exists.
type Stage int

const (
	// Archived is a release as the builder packs it: every file of the archive, and nothing that
	// is made at install.
	Archived Stage = iota
	// Installed is a release after install, which has made the session holder's environment.
	Installed
)

// IncompleteError says which files a release lacks, each named relative to the release.
type IncompleteError struct {
	Dir     string
	Missing []string
}

func (e *IncompleteError) Error() string {
	return fmt.Sprintf("the release at %s is incomplete: missing %s", e.Dir, strings.Join(e.Missing, ", "))
}

// Verify checks the release holds what its manifest says at the stage. It returns an
// *IncompleteError naming every file that is missing or cannot be run.
func (r *Release) Verify(stage Stage) error {
	var missing []string
	for _, f := range r.Manifest.GetRequiredFiles() {
		if !r.regular(f) {
			missing = append(missing, f)
		}
	}
	executables := []string{r.Manifest.GetHostProgram(), r.Manifest.GetJobTool()}
	if stage == Installed {
		executables = append(executables, r.Manifest.GetSessionHolderCommand()[0])
	}
	for _, f := range executables {
		if r.checkExecutable(f) != nil {
			missing = append(missing, f)
		}
	}
	if _, err := r.SkillsRoot(); err != nil {
		missing = append(missing, r.Manifest.GetSkills().GetRoot())
	}
	var incomplete *IncompleteError
	switch c, err := r.Catalogue(); {
	case errors.As(err, &incomplete):
		// Already named, as a required file.
	case err != nil:
		return err
	default:
		missing = append(missing, r.missingExamples(c)...)
	}
	if len(missing) == 0 {
		return nil
	}
	slices.Sort(missing)
	return &IncompleteError{Dir: r.Dir, Missing: slices.Compact(missing)}
}

// CheckPlatform says the release was built for this operating system and architecture.
func (r *Release) CheckPlatform(goos, goarch string) error {
	if r.Manifest.GetOs() != goos || r.Manifest.GetArch() != goarch {
		return fmt.Errorf("the release at %s was built for %s/%s, and this machine is %s/%s", r.Dir, r.Manifest.GetOs(), r.Manifest.GetArch(), goos, goarch)
	}
	return nil
}

func (r *Release) regular(rel string) bool {
	info, err := fs.Stat(r.fsys, rooted(r.abs(rel)))
	return err == nil && info.Mode().IsRegular()
}

func (r *Release) checkExecutable(rel string) error {
	info, err := fs.Stat(r.fsys, rooted(r.abs(rel)))
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0o111 == 0 {
		return &IncompleteError{Dir: r.Dir, Missing: []string{rel}}
	}
	return nil
}

// rooted is an absolute path as a name in an fs.FS rooted at "/".
func rooted(abs string) string { return strings.TrimPrefix(path.Clean(filepath.ToSlash(abs)), "/") }
