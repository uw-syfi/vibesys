package production

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"strings"
	"sync"
	"time"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// ProvisionFS is what Provisioner asks of the file system. Every method is one step the host can be
// interrupted at, which is how the tests break a provision at each step in turn.
type ProvisionFS interface {
	// MkdirAll makes dir and its parents.
	MkdirAll(dir string) error
	// RealPath returns path with every symbolic link resolved.
	RealPath(path string) (string, error)
	// Exists says path names anything, a link counting as itself.
	Exists(path string) (bool, error)
	// ReadFile returns the file's bytes; a missing file is an error that wraps fs.ErrNotExist.
	ReadFile(path string) ([]byte, error)
	// WriteFile makes or replaces the file, with perm.
	WriteFile(path string, data []byte, perm fs.FileMode) error
	// Mkdir makes one directory whose parent exists.
	Mkdir(dir string) error
	// MkdirTemp makes a directory under parent whose name begins with prefix, and returns its path.
	MkdirTemp(parent, prefix string) (string, error)
	// ReadDirNames lists the names of the children of dir.
	ReadDirNames(dir string) ([]string, error)
	// Rename moves a directory to a name that does not exist. It fails when to exists and is not empty.
	Rename(from, to string) error
	// RemoveAll removes path and what is under it; a path that is not there is not an error.
	RemoveAll(path string) error
}

// GitRunner runs git in dir and returns its standard output. A failure carries git's standard error.
type GitRunner interface {
	Run(ctx context.Context, dir string, args ...string) (string, error)
}

// OSProvisionFS is the real file system, as a ProvisionFS.
type OSProvisionFS struct{}

var _ ProvisionFS = OSProvisionFS{}

// MkdirAll implements ProvisionFS.
func (OSProvisionFS) MkdirAll(dir string) error { return os.MkdirAll(dir, 0o755) }

// RealPath implements ProvisionFS.
func (OSProvisionFS) RealPath(p string) (string, error) { return filepath.EvalSymlinks(p) }

// Exists implements ProvisionFS.
func (OSProvisionFS) Exists(p string) (bool, error) {
	_, err := os.Lstat(p)
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil
	}
	return err == nil, err
}

// ReadFile implements ProvisionFS.
func (OSProvisionFS) ReadFile(p string) ([]byte, error) { return os.ReadFile(p) }

// WriteFile implements ProvisionFS.
func (OSProvisionFS) WriteFile(p string, data []byte, perm fs.FileMode) error {
	return os.WriteFile(p, data, perm)
}

// Mkdir implements ProvisionFS.
func (OSProvisionFS) Mkdir(dir string) error { return os.Mkdir(dir, 0o755) }

// MkdirTemp implements ProvisionFS.
func (OSProvisionFS) MkdirTemp(parent, prefix string) (string, error) {
	return os.MkdirTemp(parent, prefix)
}

// ReadDirNames implements ProvisionFS.
func (OSProvisionFS) ReadDirNames(dir string) ([]string, error) {
	entries, err := os.ReadDir(dir)
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		names = append(names, e.Name())
	}
	return names, err
}

// Rename implements ProvisionFS.
func (OSProvisionFS) Rename(from, to string) error { return os.Rename(from, to) }

// RemoveAll implements ProvisionFS.
func (OSProvisionFS) RemoveAll(p string) error { return os.RemoveAll(p) }

// GitBound is how long one git command of a provision may take before the host gives up on it. A
// clone is the slow one; the bound only guards against a hang, and a provision that outlives it is
// refused and swept by the next request for the name.
const GitBound = 10 * time.Minute

// GitTimeouts bounds each git command by GitBound on the system clock.
type GitTimeouts struct{}

// Bound implements daemon.Bounds.
func (GitTimeouts) Bound(ctx context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(ctx, GitBound)
}

// ExecGit is the git on the host's PATH, as a GitRunner. Each command runs as a process of its own
// with its arguments as they are (no shell), under the context Bounds gives it. It keeps the host's
// own git configuration (hooks, ignore files, line endings, signing) out of what it makes.
type ExecGit struct {
	// Bounds limits each command. A nil Bounds is an unbounded command, which only a test that holds
	// the context itself leaves.
	Bounds daemon.Bounds
}

var _ GitRunner = ExecGit{}

var execGitOptions = []string{
	"-c", "user.name=VibeSys", "-c", "user.email=vibesys@localhost",
	"-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", "-c", "core.excludesFile=",
}

// Run implements GitRunner.
func (g ExecGit) Run(ctx context.Context, dir string, args ...string) (string, error) {
	if g.Bounds != nil {
		var cancel context.CancelFunc
		ctx, cancel = g.Bounds.Bound(ctx)
		defer cancel()
	}
	var stdout, stderr bytes.Buffer
	full := append(append([]string{"-C", dir}, execGitOptions...), args...)
	cmd := exec.CommandContext(ctx, "git", full...)
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("git %s: %w: %s", args[0], err, strings.TrimSpace(stderr.String()))
	}
	return stdout.String(), nil
}

const (
	stagingPrefix = ".provision-"
	// markerName is the file in the new repository's git directory that records the request that made
	// it, so the same request asked again gets the same answer.
	markerName = "vibesys-provision"
)

// Provisioner makes repositories under the host's repositories directory. It is a daemon.Provisioner.
//
// A repository is built whole in a staging directory beside its final place and renamed into it, so
// a name holds a complete repository or nothing. It never overwrites. The same request (id, name and
// source) asked again answers the same success; any other request for a taken name is refused. What
// a crash leaves in staging is swept by the next request for the same name.
//
// An example with an external source is cloned at its pinned revision, without the clone's history,
// and the example's own (vendored) files are written over it: vendored paths win.
type Provisioner struct {
	Trees daemon.ExampleTrees
	FS    ProvisionFS
	Git   GitRunner
	// Default is the host's repositories directory, used when a request names no parent. A leading
	// "~" is this host's home.
	Default string
	// Home is the home directory of the user the host runs as, which "~" stands for.
	Home string

	mu    sync.Mutex
	locks map[string]*sync.Mutex
}

var _ daemon.Provisioner = (*Provisioner)(nil)

// lock serializes requests for one repository path, so exactly one of two concurrent requests for a
// name makes it and the other sees it made. It returns the unlock.
func (p *Provisioner) lock(final string) func() {
	p.mu.Lock()
	if p.locks == nil {
		p.locks = map[string]*sync.Mutex{}
	}
	l := p.locks[final]
	if l == nil {
		l = &sync.Mutex{}
		p.locks[final] = l
	}
	p.mu.Unlock()
	l.Lock()
	return l.Unlock
}

func provisionRefusal(reason pb.ProvisionRefusal, format string, args ...any) *pb.ProvisionRepositoryResponse {
	return daemon.RefuseProvision(reason, fmt.Sprintf(format, args...))
}

// parent resolves the directory repositories go in: the request's, or the default, with "~" expanded
// here and nowhere else. It refuses a relative path and any path with a ".." in it.
func (p *Provisioner) parent(requested string) (string, error) {
	dir := requested
	if dir == "" {
		dir = p.Default
	}
	for _, part := range strings.Split(filepath.ToSlash(dir), "/") {
		if part == ".." { // Before ~ is expanded: joining would resolve it away.
			return "", fmt.Errorf("%q goes up through ..", dir)
		}
	}
	switch {
	case dir == "~":
		dir = p.Home
	case strings.HasPrefix(dir, "~/"):
		dir = filepath.Join(p.Home, dir[2:])
	case strings.HasPrefix(dir, "~"):
		return "", fmt.Errorf("%q: only the home directory of the host's user can be written as ~", dir)
	}
	if !filepath.IsAbs(dir) {
		return "", fmt.Errorf("%q is not an absolute path", dir)
	}
	return filepath.Clean(dir), nil
}

// Provision implements daemon.Provisioner.
func (p *Provisioner) Provision(ctx context.Context, req *pb.ProvisionRepositoryRequest) (*pb.ProvisionRepositoryResponse, error) {
	name := req.GetName()
	if !daemon.ValidRepositoryName(name) {
		return provisionRefusal(pb.ProvisionRefusal_PROVISION_REFUSAL_NAME_INVALID, "name %q is not made of lowercase letters, digits and dashes", name), nil
	}
	dir, err := p.parent(req.GetParentDirectory())
	if err != nil {
		return provisionRefusal(pb.ProvisionRefusal_PROVISION_REFUSAL_PARENT_NOT_WRITABLE, "%v", err), nil
	}
	if err := p.FS.MkdirAll(dir); err != nil {
		return provisionRefusal(pb.ProvisionRefusal_PROVISION_REFUSAL_PARENT_NOT_WRITABLE, "%s cannot be made: %v", dir, err), nil
	}
	resolved, err := p.FS.RealPath(dir)
	if err != nil {
		return provisionRefusal(pb.ProvisionRefusal_PROVISION_REFUSAL_PARENT_NOT_WRITABLE, "%s cannot be resolved: %v", dir, err), nil
	}
	final := filepath.Join(resolved, name)
	defer p.lock(final)()

	want := requestMarker(req)
	if again, err := p.existing(ctx, final, want); again != nil || err != nil {
		return again, err
	}
	var source daemon.ExampleSource
	if example := req.GetExample(); example != "" {
		if source, err = p.Trees.Source(ctx, example); err != nil {
			var gone *daemon.ExampleUnavailable
			switch {
			case errors.As(err, &gone) && gone.Reason == daemon.ExampleNotFound:
				return provisionRefusal(pb.ProvisionRefusal_PROVISION_REFUSAL_EXAMPLE_NOT_FOUND, "example %q is not in this release", example), nil
			case errors.As(err, &gone):
				return provisionRefusal(pb.ProvisionRefusal_PROVISION_REFUSAL_COPY_FAILED, "example %q cannot be made: %s", example, gone.Detail), nil
			}
			return nil, err
		}
		if c := source.Clone; c != nil && (c.Revision == "" || c.URL == "") {
			return provisionRefusal(pb.ProvisionRefusal_PROVISION_REFUSAL_COPY_FAILED, "example %q is cloned from elsewhere and its registry entry pins no revision", example), nil
		}
	}

	if err := p.sweep(resolved, name); err != nil {
		return provisionRefusal(pb.ProvisionRefusal_PROVISION_REFUSAL_COPY_FAILED, "an earlier attempt's leftovers cannot be removed: %v", err), nil
	}
	stage, err := p.FS.MkdirTemp(resolved, stagingPrefix+name+".")
	if err != nil {
		return provisionRefusal(pb.ProvisionRefusal_PROVISION_REFUSAL_PARENT_NOT_WRITABLE, "%s cannot be written: %v", resolved, err), nil
	}
	made := false
	defer func() {
		if !made {
			_ = p.FS.RemoveAll(stage) // A leftover is swept by the next request for this name.
		}
	}()

	if out, err := p.build(ctx, stage, req, source, want); out != nil || err != nil {
		return out, err
	}
	commit, err := p.Git.Run(ctx, stage, "rev-parse", "HEAD")
	if err != nil {
		return p.copyFailed(ctx, err)
	}
	if err := p.FS.Rename(stage, final); err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		// Another request, or another process, made the name between the check and the rename.
		if again, aerr := p.existing(ctx, final, want); again != nil || aerr != nil {
			return again, aerr
		}
		return p.copyFailed(ctx, err)
	}
	made = true
	return provisioned(final, strings.TrimSpace(commit)), nil
}

func requestMarker(req *pb.ProvisionRepositoryRequest) string {
	source := "empty"
	if req.GetExample() != "" {
		source = "example " + req.GetExample()
	}
	return req.GetRequestId() + "\n" + req.GetName() + "\n" + source + "\n"
}

func provisioned(repository, commit string) *pb.ProvisionRepositoryResponse {
	return &pb.ProvisionRepositoryResponse{Outcome: &pb.ProvisionRepositoryResponse_Provisioned{
		Provisioned: &pb.Provisioned{Repository: repository, Commit: commit},
	}}
}

// copyFailed is the refusal for a step that failed, or the context's error when the caller gave up.
func (p *Provisioner) copyFailed(ctx context.Context, err error) (*pb.ProvisionRepositoryResponse, error) {
	if ctx.Err() != nil {
		return nil, ctx.Err()
	}
	return provisionRefusal(pb.ProvisionRefusal_PROVISION_REFUSAL_COPY_FAILED, "the repository could not be made: %v", err), nil
}

// existing answers a request for a name that is taken: the same success when the request is the one
// that made it, NAME_TAKEN otherwise. It answers nil, nil when the name is free.
func (p *Provisioner) existing(ctx context.Context, final, want string) (*pb.ProvisionRepositoryResponse, error) {
	taken, err := p.FS.Exists(final)
	if err != nil {
		return p.copyFailed(ctx, err)
	}
	if !taken {
		return nil, nil
	}
	got, err := p.FS.ReadFile(filepath.Join(final, ".git", markerName))
	if err == nil && string(got) == want {
		commit, err := p.Git.Run(ctx, final, "rev-parse", "HEAD")
		if err != nil {
			return p.copyFailed(ctx, err)
		}
		return provisioned(final, strings.TrimSpace(commit)), nil
	}
	res := provisionRefusal(pb.ProvisionRefusal_PROVISION_REFUSAL_NAME_TAKEN, "%s exists", final)
	res.GetRefused().ExistingRepository = final
	return res, nil
}

// sweep removes the staging directories an earlier request for name left when it was interrupted.
func (p *Provisioner) sweep(dir, name string) error {
	names, err := p.FS.ReadDirNames(dir)
	if err != nil {
		return err
	}
	for _, n := range names {
		if strings.HasPrefix(n, stagingPrefix+name+".") {
			if err := p.FS.RemoveAll(filepath.Join(dir, n)); err != nil {
				return err
			}
		}
	}
	return nil
}

// build fills stage with the example (or nothing), makes it a repository with one commit and records
// the marker. A refusal or error is returned; nil, nil is success.
func (p *Provisioner) build(ctx context.Context, stage string, req *pb.ProvisionRepositoryRequest, source daemon.ExampleSource, marker string) (*pb.ProvisionRepositoryResponse, error) {
	example := req.GetExample()
	if c := source.Clone; c != nil {
		if strings.HasPrefix(c.URL, "-") || strings.HasPrefix(c.Revision, "-") {
			return provisionRefusal(pb.ProvisionRefusal_PROVISION_REFUSAL_COPY_FAILED, "example %q has a clone URL or revision that begins with a dash", example), nil
		}
		if _, err := p.Git.Run(ctx, stage, "clone", "--no-checkout", "--template=", "--", c.URL, "."); err != nil {
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			return provisionRefusal(pb.ProvisionRefusal_PROVISION_REFUSAL_NEEDS_NETWORK, "example %q cannot be cloned: %v", example, err), nil
		}
		if _, err := p.Git.Run(ctx, stage, "checkout", "--detach", c.Revision, "--"); err != nil {
			return p.copyFailed(ctx, fmt.Errorf("example %q has no revision %s: %w", example, c.Revision, err))
		}
		if err := p.FS.RemoveAll(path.Join(stage, ".git")); err != nil { // The clone's history is not the new repository's.
			return p.copyFailed(ctx, err)
		}
	}
	if source.Vendored != nil {
		if err := p.copyTree(source.Vendored, stage); err != nil {
			return p.copyFailed(ctx, err)
		}
	}
	for _, args := range [][]string{
		{"init", "-b", "main", "--template="},
		{"add", "-A"},
		{"commit", "--allow-empty", "-m", "Initial commit"},
	} {
		if _, err := p.Git.Run(ctx, stage, args...); err != nil {
			return p.copyFailed(ctx, err)
		}
	}
	if err := p.FS.WriteFile(filepath.Join(stage, ".git", markerName), []byte(marker), 0o644); err != nil {
		return p.copyFailed(ctx, err)
	}
	return nil, nil
}

// copyTree writes src into dst, over what is there. A link, a device or a .git in src is refused: the
// trees are the release's own files.
func (p *Provisioner) copyTree(src fs.FS, dst string) error {
	return fs.WalkDir(src, ".", func(name string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if name == "." {
			return nil
		}
		if path.Base(name) == ".git" {
			return fmt.Errorf("%s: a source tree has no .git", name)
		}
		target := filepath.Join(dst, filepath.FromSlash(name))
		switch {
		case d.IsDir():
			if ok, err := p.FS.Exists(target); err != nil || ok {
				return err
			}
			return p.FS.Mkdir(target)
		case d.Type().IsRegular():
			info, err := d.Info()
			if err != nil {
				return err
			}
			data, err := fs.ReadFile(src, name)
			if err != nil {
				return err
			}
			return p.FS.WriteFile(target, data, info.Mode().Perm())
		}
		return fmt.Errorf("%s: only files and directories are copied", name)
	})
}
