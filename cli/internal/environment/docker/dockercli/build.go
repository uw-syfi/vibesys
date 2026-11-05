package dockercli

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os/exec"
	"strings"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
)

// tailLines is how many of the last lines of a failed build its error carries.
const tailLines = 25

// maxError bounds the end of a build's log that an error carries.
const maxError = 8 << 10

// maxLine bounds the part of a line the tail keeps, so that a build that prints without ever
// ending a line cannot use all memory. The end of an over-long line is kept.
const maxLine = 64 << 10

// Build implements docker.Engine. The build context is passed as a tar on standard input, so the
// build can see no file but the context's.
func (e *Engine) Build(ctx context.Context, spec docker.BuildSpec, log io.Writer) error {
	if !tagPattern.MatchString(spec.Tag) || len(spec.Context) == 0 {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("dockercli: a build needs a tag (got %q) and a build context", spec.Tag)}
	}
	args := []string{"build", "--progress=plain", "--tag", spec.Tag}
	for _, k := range sortedKeys(spec.Labels) {
		if err := checkLabel(k, spec.Labels[k]); err != nil {
			return err
		}
		args = append(args, "--label", k+"="+spec.Labels[k])
	}
	args = append(args, "-")
	cmd := e.command(ctx, &Allocation{Name: BuildName(spec.Tag), Scope: spec.Labels[docker.LabelScope], Build: true}, args...)
	cmd.Stdin = bytes.NewReader(spec.Context)
	out := &tail{w: log}
	// One writer for both streams: the build's progress is one ordered log.
	cmd.Stdout, cmd.Stderr = out, out
	err := cmd.Run()
	if err == nil {
		return nil
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	var exit *exec.ExitError
	if !asExit(err, &exit) {
		return unavailable("cannot run %q to build: %v; %s", e.program, err, hint)
	}
	if e.shell.Lost(exit.ExitCode()) {
		return unavailable("the build of the task's image on %s ended with the transport's own exit code %d (a lost connection, or a command that exited with it); the end of the build's output:\n%s", e.shell.Where(), exit.ExitCode(), out.last())
	}
	return unavailable("building the task's image failed (exit %d); the end of the build's output:\n%s", exit.ExitCode(), out.last())
}

// Save writes the image as a docker archive (docker save) to w, streaming: an image is as large as
// it is, so no bound is put on the copy but the caller's.
func (e *Engine) Save(ctx context.Context, image string, w io.Writer) error {
	if image == "" || strings.HasPrefix(image, "-") {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("dockercli: %q is not an image reference", image)}
	}
	cmd := e.command(ctx, nil, "save", image)
	cmd.Stdout = w
	errs := &tail{w: io.Discard}
	cmd.Stderr = errs
	err := cmd.Run()
	if err == nil {
		return nil
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	var exit *exec.ExitError
	if !asExit(err, &exit) {
		return unavailable("cannot run %q to save the image %s: %v; %s", e.program, image, err, hint)
	}
	return unavailable("docker save %s failed (exit %d): %s", image, exit.ExitCode(), clip(errs.last()))
}

// BuildName is the unique name of a build of the tag, as an Allocator is given it: a build of one
// tag runs at most once at a time in one process, and its name says which image it builds.
func BuildName(tag string) string {
	return BuildPrefix + strings.ReplaceAll(strings.ReplaceAll(tag, ":", "-"), "/", "-")
}

// BuildPrefix starts the name of every build.
const BuildPrefix = docker.Owner + "-build-"

// tail passes a log on and remembers its last lines.
type tail struct {
	w io.Writer

	mu    sync.Mutex
	lines []string
	part  string
}

func (t *tail) Write(p []byte) (int, error) {
	t.mu.Lock()
	text := t.part + string(p)
	parts := strings.Split(text, "\n")
	t.part = clipEnd(parts[len(parts)-1], maxLine)
	for _, line := range parts[:len(parts)-1] {
		t.lines = append(t.lines, clipEnd(line, maxLine))
	}
	if extra := len(t.lines) - tailLines; extra > 0 {
		t.lines = t.lines[extra:]
	}
	t.mu.Unlock()
	// A log that cannot be written must not fail the build: the build's result is the image.
	_, _ = t.w.Write(p)
	return len(p), nil
}

// clipEnd keeps at most n bytes of the end of s.
func clipEnd(s string, n int) string {
	if len(s) > n {
		return s[len(s)-n:]
	}
	return s
}

func (t *tail) last() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	lines := t.lines
	if t.part != "" {
		lines = append(append([]string(nil), lines...), t.part)
	}
	if extra := len(lines) - tailLines; extra > 0 {
		lines = lines[extra:]
	}
	return strings.ToValidUTF8(clipEnd(strings.Join(lines, "\n"), maxError), "")
}
