//go:build linux || darwin

// Package localsif is the apptainer.Converter of the caller's machine: its apptainer program,
// found on PATH, builds a SIF of a docker archive in a directory of its own under the operator's
// local scratch directory, with APPTAINER_TMPDIR and APPTAINER_CACHEDIR there too, renames the SIF
// into the scratch directory's sif directory, where it is kept, and removes its own directory when
// it ends. A build is one process group, which ends whole: no process of it outlives Build.
package localsif

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
)

// Program is the apptainer program's name, looked up on PATH.
const Program = "apptainer"

// tailBytes bounds the end of a failed build's output that its error carries.
const tailBytes = 4 << 10

// Converter runs the machine's apptainer program. Create it with New.
type Converter struct {
	program string
	timers  clock.Timers
	// grace is how long a cancelled build has after SIGTERM before every process of it is killed.
	grace time.Duration
}

var _ apptainer.Converter = (*Converter)(nil)

// New finds the apptainer program in the directories of pathList, the daemon's PATH, as the shell
// would, or says to install it. timers measure grace: how long a cancelled build has to end after
// SIGTERM before every process of it is killed, the operator's stop_grace_seconds, or
// environment.DefaultGrace when it is zero.
func New(pathList string, timers clock.Timers, grace time.Duration) (*Converter, error) {
	if grace <= 0 {
		grace = environment.DefaultGrace
	}
	for _, dir := range filepath.SplitList(pathList) {
		if !filepath.IsAbs(dir) {
			// A relative directory would be the daemon's working directory's, which is no one's choice.
			continue
		}
		path := filepath.Join(dir, Program)
		if info, err := os.Stat(path); err == nil && info.Mode().IsRegular() && info.Mode()&0o111 != 0 {
			return &Converter{program: path, timers: timers, grace: grace}, nil
		}
	}
	return nil, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(
		"localsif: the apptainer placement converts images to SIFs on this machine with its apptainer program, "+
			"and there is none on the daemon's PATH (%s); install Apptainer "+
			"(https://apptainer.org/docs/admin/main/installation.html) and restart the daemon", pathList)}
}

// Free implements apptainer.Converter.
func (c *Converter) Free(ctx context.Context, dir string) (int64, error) {
	if err := ctx.Err(); err != nil {
		return 0, err
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return 0, unavailable("cannot make the scratch directory %s: %v", dir, err)
	}
	var st syscall.Statfs_t
	if err := syscall.Statfs(dir, &st); err != nil {
		return 0, unavailable("cannot read the free space of %s: %v", dir, err)
	}
	// Bsize is an int64 on linux and a uint32 on darwin.
	return int64(st.Bavail) * int64(st.Bsize), nil
}

// Lock implements apptainer.Converter with flock(2) on the file of the name in dir. The kernel lets
// the lock go when the process ends, however it ends, so a crash leaves no lock held. The file stays:
// removing it would let a process that opened it before take a lock that a later opener of the new
// file of the name does not see. The directory is a local disk's (local_scratch_dir), whose locks
// hold between the processes of this machine.
func (c *Converter) Lock(ctx context.Context, dir, name string) (io.Closer, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if err := checkName(name); err != nil {
		return nil, err
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, unavailable("cannot make the scratch directory %s: %v", dir, err)
	}
	path := filepath.Join(dir, name)
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return nil, unavailable("cannot open the lock %s: %v", path, err)
	}
	for {
		err = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if !errors.Is(err, syscall.EINTR) {
			break
		}
	}
	switch {
	case err == nil:
		return f, nil
	case errors.Is(err, syscall.EWOULDBLOCK):
		_ = f.Close()
		return nil, apptainer.ErrConverting
	default:
		_ = f.Close()
		return nil, unavailable("cannot lock %s: %v", path, err)
	}
}

// keptDir is the directory under the scratch directory that keeps the SIFs.
const keptDir = "sif"

// workPrefix starts the name of a build's own directory under the scratch directory.
const workPrefix = "vibesys-sif."

// buildScript builds the SIF of the docker archive on its standard input with the apptainer
// program $2, in its own directory $4 under $1, and renames it to $1/sif/$3 once it is whole;
// apptainer's own output goes to standard error. Build removes the directory once every process
// of the build has ended; what a build of a process that died left is removed by a later one after
// a day, and so is a kept SIF. On SIGTERM the shell waits for apptainer to end before it exits, so
// that the end of the group's leader is the end of the build.
const buildScript = `set -eu
find "$1" -maxdepth 1 -name 'vibesys-sif.*' -mmin +1440 -exec rm -rf {} + 2>/dev/null || true
mkdir -p "$1/sif"
find "$1/sif" -maxdepth 1 -type f -mmin +1440 -exec rm -f {} + 2>/dev/null || true
d="$4"
trap 'exit 1' HUP INT TERM
mkdir "$d/tmp" "$d/cache"
cat > "$d/image.tar"
APPTAINER_TMPDIR="$d/tmp" APPTAINER_CACHEDIR="$d/cache" "$2" build --force "$d/image.sif" "docker-archive:$d/image.tar" >&2
mv -f "$d/image.sif" "$1/sif/$3"`

// Build implements apptainer.Converter. The build runs as a process group of its own. A cancelled
// build is sent SIGTERM, every process of it, and once its leader has ended, or the grace is over,
// whatever is left of the group is killed; a build that ended by itself has what it left of the
// group killed too. Build returns only then, and removes the build's directory, so a conversion cut
// short by its bound leaves no apptainer or mksquashfs writing to the scratch directory.
func (c *Converter) Build(ctx context.Context, dir string, archive io.Reader, name string) error {
	if err := checkName(name); err != nil {
		return err
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return unavailable("cannot make the scratch directory %s: %v", dir, err)
	}
	work, err := os.MkdirTemp(dir, workPrefix)
	if err != nil {
		return unavailable("cannot make the build's directory in %s: %v", dir, err)
	}
	// Deferred first, so it runs last: after every process of the build has been killed.
	defer func() { _ = os.RemoveAll(work) }()
	cmd := exec.Command("/bin/sh", "-c", buildScript, "vs-sif", dir, c.program, name, work)
	cmd.Stdin = archive
	errs := &tail{}
	cmd.Stdout, cmd.Stderr = errs, errs
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	// Bounds the wait for the output and the archive's copy once the leader has ended: a process
	// that left the group may hold the output open.
	cmd.WaitDelay = c.grace
	if err := cmd.Start(); err != nil {
		return unavailable("cannot start %s: %v", c.program, err)
	}
	// The leader's process id is the group's, since it leads a group of its own.
	pgid := cmd.Process.Pid
	exited := make(chan error, 1)
	go func() { exited <- cmd.Wait() }()
	// The group no longer existing is the usual answer to a signal, for a build that ended.
	defer func() { _ = syscall.Kill(-pgid, syscall.SIGKILL) }()
	select {
	case err = <-exited:
	case <-ctx.Done():
		_ = syscall.Kill(-pgid, syscall.SIGTERM)
		grace, stop := c.timers.Timeout(ctx, c.grace)
		select {
		case err = <-exited:
		case <-grace:
			_ = syscall.Kill(-pgid, syscall.SIGKILL)
			err = <-exited
		}
		stop()
	}
	if err == nil {
		return nil
	}
	if ctx.Err() != nil {
		return context.Cause(ctx)
	}
	return unavailable("%s build failed (%v): %s", c.program, err, errs.String())
}

// Open implements apptainer.Converter.
func (c *Converter) Open(ctx context.Context, dir, name string) (io.ReadCloser, bool, error) {
	if err := checkName(name); err != nil {
		return nil, false, err
	}
	if err := ctx.Err(); err != nil {
		return nil, false, err
	}
	f, err := os.Open(filepath.Join(dir, keptDir, name))
	switch {
	case err == nil:
		return f, true, nil
	case errors.Is(err, fs.ErrNotExist):
		return nil, false, nil
	default:
		return nil, false, unavailable("cannot open the kept SIF: %v", err)
	}
}

// Remove implements apptainer.Converter.
func (c *Converter) Remove(ctx context.Context, dir, name string) error {
	if err := checkName(name); err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	if err := os.Remove(filepath.Join(dir, keptDir, name)); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return unavailable("cannot remove the kept SIF: %v", err)
	}
	return nil
}

func checkName(name string) error {
	if name == "" || strings.ContainsRune(name, '/') || name == "." || name == ".." {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("localsif: %q is not a file name", name)}
	}
	return nil
}

// Describe implements apptainer.Converter: what the program says of its version, and where it is.
func (c *Converter) Describe(ctx context.Context) (string, error) {
	out, err := exec.CommandContext(ctx, c.program, "version").Output()
	if err != nil {
		return "", unavailable("%s version failed: %v", c.program, err)
	}
	return fmt.Sprintf("apptainer %s (%s)", strings.TrimSpace(string(out)), c.program), nil
}

func unavailable(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureUnavailable, Message: "localsif: " + fmt.Sprintf(format, args...)}
}

// tail keeps the end of what is written to it.
type tail struct {
	mu  sync.Mutex
	buf []byte
}

func (t *tail) Write(p []byte) (int, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.buf = append(t.buf, p...)
	if extra := len(t.buf) - tailBytes; extra > 0 {
		t.buf = t.buf[extra:]
	}
	return len(p), nil
}

func (t *tail) String() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return strings.ToValidUTF8(strings.TrimSpace(string(t.buf)), "")
}
