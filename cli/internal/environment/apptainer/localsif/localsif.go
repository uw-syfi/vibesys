//go:build linux || darwin

// Package localsif is the apptainer.Converter of the caller's machine: its apptainer program,
// found on PATH, builds a SIF of a docker archive in a directory of its own under the operator's
// local scratch directory, with APPTAINER_TMPDIR and APPTAINER_CACHEDIR there too, and removes the
// directory when it ends.
package localsif

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
)

// Program is the apptainer program's name, looked up on PATH.
const Program = "apptainer"

// stopGrace is how long a cancelled build has to remove its directory after SIGTERM before its
// processes are killed.
const stopGrace = 30 * time.Second

// tailBytes bounds the end of a failed build's output that its error carries.
const tailBytes = 4 << 10

// Converter runs the machine's apptainer program. Create it with New.
type Converter struct{ program string }

var _ apptainer.Converter = (*Converter)(nil)

// New finds the apptainer program in the directories of pathList, the daemon's PATH, as the shell
// would, or says to install it.
func New(pathList string) (*Converter, error) {
	for _, dir := range filepath.SplitList(pathList) {
		if !filepath.IsAbs(dir) {
			// A relative directory would be the daemon's working directory's, which is no one's choice.
			continue
		}
		path := filepath.Join(dir, Program)
		if info, err := os.Stat(path); err == nil && info.Mode().IsRegular() && info.Mode()&0o111 != 0 {
			return &Converter{program: path}, nil
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

// buildScript builds the SIF of the docker archive on its standard input with the apptainer
// program $2, in a directory of its own under $1, and writes the SIF on its standard output once
// it is whole; apptainer's own output goes to standard error. The directory is removed when the
// script ends, however it ends but killed outright; what a killed build left is removed by a later
// one after a day.
const buildScript = `set -eu
find "$1" -maxdepth 1 -name 'vibesys-sif.*' -mmin +1440 -exec rm -rf {} + 2>/dev/null || true
d=$(mktemp -d "$1/vibesys-sif.XXXXXX")
trap 'rm -rf "$d"' EXIT
trap 'exit 1' HUP INT TERM
mkdir "$d/tmp" "$d/cache"
cat > "$d/image.tar"
APPTAINER_TMPDIR="$d/tmp" APPTAINER_CACHEDIR="$d/cache" "$2" build --force "$d/image.sif" "docker-archive:$d/image.tar" >&2
cat "$d/image.sif"`

// Build implements apptainer.Converter. A cancelled build is sent SIGTERM, every process of it,
// so that it removes its directory, and is killed after stopGrace.
func (c *Converter) Build(ctx context.Context, dir string, archive io.Reader, sif io.Writer) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return unavailable("cannot make the scratch directory %s: %v", dir, err)
	}
	cmd := exec.CommandContext(ctx, "/bin/sh", "-c", buildScript, "vs-sif", dir, c.program)
	cmd.Stdin, cmd.Stdout = archive, sif
	errs := &tail{}
	cmd.Stderr = errs
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	cmd.Cancel = func() error { return syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM) }
	cmd.WaitDelay = stopGrace
	err := cmd.Run()
	if err == nil {
		return nil
	}
	if ctx.Err() != nil {
		return context.Cause(ctx)
	}
	return unavailable("%s build failed (%v): %s", c.program, err, errs.String())
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
