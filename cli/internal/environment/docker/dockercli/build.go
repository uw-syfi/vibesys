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

// Build implements docker.Engine. The Dockerfile is the whole build context: it is passed on
// standard input, so the build can see no other file.
func (e *Engine) Build(ctx context.Context, spec docker.BuildSpec, log io.Writer) error {
	if !tagPattern.MatchString(spec.Tag) || len(spec.Dockerfile) == 0 {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("dockercli: a build needs a tag (got %q) and a Dockerfile", spec.Tag)}
	}
	args := []string{"build", "--progress=plain", "--tag", spec.Tag}
	for _, k := range sortedKeys(spec.Labels) {
		if err := checkLabel(k, spec.Labels[k]); err != nil {
			return err
		}
		args = append(args, "--label", k+"="+spec.Labels[k])
	}
	args = append(args, "-")
	cmd := exec.CommandContext(ctx, e.program, args...)
	cmd.Stdin = bytes.NewReader(spec.Dockerfile)
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
	return unavailable("building the task's image failed (exit %d); the end of the build's output:\n%s", exit.ExitCode(), out.last())
}

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
