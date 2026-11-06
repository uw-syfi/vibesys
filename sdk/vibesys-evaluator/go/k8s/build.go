package k8s

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"syscall"
	"time"
)

// An image is tagged with a digest of its build context, so a context that did not change names
// an image the registry already has, and BuildKit's layer cache (kept by the builder between
// jobs, with the Dockerfiles' cache mounts) makes its rebuild a no-op.

// BuildError means an image did not build; the message names it and ends with the build log's tail.
type BuildError struct{ Message string }

func (e *BuildError) Error() string { return e.Message }

// Build is one image to build and push.
type Build struct {
	Context string // a directory holding the Dockerfile
	Image   string // registry/repository:tag, pushed when built
}

// Builder builds images.
type Builder interface {
	// Build builds and pushes every image at once and returns each image's build seconds. It
	// returns a *BuildError naming the first image that failed.
	Build(ctx context.Context, builds []Build, timeout time.Duration, logDir string) (map[string]float64, error)
}

// Files tools generate are not part of a context's identity (vibesys-cli leaves them out of the
// task version for the same reason).
func ignoredDir(name string) bool  { return name == "__pycache__" || name == ".git" }
func ignoredFile(name string) bool { return strings.HasSuffix(name, ".pyc") }

// contextFiles lists the files of a build context (symlinks to directories included as entries,
// not followed), sorted by relative path.
func contextFiles(root string) ([]string, error) {
	var found []string
	var walk func(dir string) error
	walk = func(dir string) error {
		entries, err := os.ReadDir(dir)
		if err != nil {
			return err
		}
		for _, entry := range entries {
			full := filepath.Join(dir, entry.Name())
			isLink := entry.Type()&os.ModeSymlink != 0
			isDir := entry.IsDir()
			if isLink {
				if info, err := os.Stat(full); err == nil && info.IsDir() {
					found = append(found, full) // a link to a directory is an entry, never entered
					continue
				}
			}
			switch {
			case isDir:
				if !ignoredDir(entry.Name()) {
					if err := walk(full); err != nil {
						return err
					}
				}
			case !ignoredFile(entry.Name()):
				found = append(found, full)
			}
		}
		return nil
	}
	if err := walk(root); err != nil {
		return nil, err
	}
	rel := func(p string) string { r, _ := filepath.Rel(root, p); return filepath.ToSlash(r) }
	sort.Slice(found, func(i, j int) bool { return rel(found[i]) < rel(found[j]) })
	return found, nil
}

// TreeDigest is 16 hex digits over every file's relative path, mode (executable or not) and
// content. It is the digest the Python implementation computed, so an image built by either is
// found by the other.
func TreeDigest(root string) (string, error) {
	files, err := contextFiles(root)
	if err != nil {
		return "", err
	}
	digest := sha256.New()
	for _, path := range files {
		rel, _ := filepath.Rel(root, path)
		digest.Write(append([]byte(filepath.ToSlash(rel)), 0))
		info, err := os.Lstat(path)
		if err != nil {
			return "", err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			target, err := os.Readlink(path)
			if err != nil {
				return "", err
			}
			digest.Write(append([]byte("L"), target...))
			continue
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return "", err
		}
		sum := sha256.Sum256(data)
		digest.Write(sum[:])
		if syscall.Access(path, 1) == nil {
			digest.Write([]byte("x"))
		} else {
			digest.Write([]byte("-"))
		}
	}
	return hex.EncodeToString(digest.Sum(nil))[:16], nil
}

// Stage copies a context without the generated files, so the build sees what the digest covers.
func Stage(source, into string) (string, error) {
	files, err := contextFiles(source)
	if err != nil {
		return "", err
	}
	for _, path := range files {
		rel, _ := filepath.Rel(source, path)
		target := filepath.Join(into, rel)
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return "", err
		}
		info, err := os.Lstat(path)
		if err != nil {
			return "", err
		}
		if info.Mode()&os.ModeSymlink != 0 {
			link, err := os.Readlink(path)
			if err != nil {
				return "", err
			}
			if err := os.Symlink(link, target); err != nil {
				return "", err
			}
			continue
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return "", err
		}
		if err := os.WriteFile(target, data, info.Mode().Perm()); err != nil {
			return "", err
		}
	}
	return into, os.MkdirAll(into, 0o755)
}

var repoChars = regexp.MustCompile(`[^a-z0-9._-]+`)

// ImageName is registry/vse/<app>/<component>:<digest>.
func ImageName(registry, appSlug, component, digest string) string {
	repository := strings.Trim(repoChars.ReplaceAllString(strings.ToLower(component), "-"), "-._")
	if repository == "" {
		repository = "image"
	}
	return fmt.Sprintf("%s/vse/%s/%s:%s", registry, appSlug, repository, digest)
}

// BuildctlBuilder runs buildctl against the BuildKit daemon at Address, pushing to an insecure
// registry.
type BuildctlBuilder struct {
	Address string
	Clock   Clock
	// Buildctl is the binary; "buildctl" when empty.
	Buildctl string
}

// Argv is the buildctl command line of one build.
func (b BuildctlBuilder) Argv(build Build) []string {
	binary := b.Buildctl
	if binary == "" {
		binary = "buildctl"
	}
	return []string{
		binary, "--addr", b.Address, "build",
		"--frontend", "dockerfile.v0",
		"--local", "context=" + build.Context,
		"--local", "dockerfile=" + build.Context,
		"--output", "type=image,name=" + build.Image + ",push=true,registry.insecure=true",
	}
}

type running struct {
	build   Build
	cmd     *exec.Cmd
	log     *os.File
	logPath string
	done    chan error
}

// Build implements Builder: every build starts at once, and they are awaited in order.
func (b BuildctlBuilder) Build(ctx context.Context, builds []Build, timeout time.Duration, logDir string) (map[string]float64, error) {
	if err := os.MkdirAll(logDir, 0o755); err != nil {
		return nil, err
	}
	clock := b.Clock
	if clock == nil {
		clock = SystemClock{}
	}
	started := clock.Now()
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	var all []*running
	defer func() {
		for _, r := range all {
			if r.cmd.Process != nil {
				_ = r.cmd.Process.Kill()
			}
			<-r.done
			r.log.Close()
		}
	}()
	for index, build := range builds {
		logPath := filepath.Join(logDir, fmt.Sprintf("build-%d.log", index))
		log, err := os.Create(logPath)
		if err != nil {
			return nil, err
		}
		argv := b.Argv(build)
		cmd := exec.Command(argv[0], argv[1:]...)
		cmd.Stdout, cmd.Stderr = log, log
		r := &running{build: build, cmd: cmd, log: log, logPath: logPath, done: make(chan error, 1)}
		if err := cmd.Start(); err != nil {
			log.Close()
			return nil, &BuildError{fmt.Sprintf("build of %s: cannot start %s: %v", build.Image, argv[0], err)}
		}
		go func() { r.done <- cmd.Wait() }()
		all = append(all, r)
	}
	seconds := map[string]float64{}
	for _, r := range all {
		var err error
		select {
		case err = <-r.done:
			r.done <- err // read again by the cleanup
		case <-ctx.Done():
			if errors.Is(ctx.Err(), context.DeadlineExceeded) {
				return nil, &BuildError{fmt.Sprintf("build of %s did not finish in %.0fs; log tail:\n%s",
					r.build.Image, timeout.Seconds(), tail(r.logPath, 3000))}
			}
			return nil, ctx.Err()
		}
		if err != nil {
			code := -1
			var exit *exec.ExitError
			if errors.As(err, &exit) {
				code = exit.ExitCode()
			}
			return nil, &BuildError{fmt.Sprintf("build of %s from %s failed (buildctl exit %d); log tail:\n%s",
				r.build.Image, filepath.Base(r.build.Context), code, tail(r.logPath, 3000))}
		}
		seconds[r.build.Image] = float64(int(clock.Now().Sub(started).Seconds()*10+0.5)) / 10
	}
	return seconds, nil
}

func tail(path string, size int) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	if len(data) > size {
		data = data[len(data)-size:]
	}
	return string(bytes.ToValidUTF8(data, []byte("?")))
}
