package devlaunch

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

// OSRunner runs real processes. It starts only the child it is asked for and waits for it; a
// timeout stops that child alone.
type OSRunner struct{ Root string }

// Run implements Runner.
func (r OSRunner) Run(c Cmd) ([]byte, error) {
	ctx := context.Background()
	if c.Timeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, c.Timeout)
		defer cancel()
	}
	cmd := exec.CommandContext(ctx, c.Argv[0], c.Argv[1:]...)
	cmd.Dir = filepath.Join(r.Root, c.Dir)
	if filepath.IsAbs(c.Dir) {
		cmd.Dir = c.Dir
	}
	if c.Stdin != nil {
		cmd.Stdin = bytes.NewReader(c.Stdin)
	}
	cmd.Env = append(os.Environ(), c.Env...)
	var stdout, stderr bytes.Buffer
	if c.Passthrough {
		cmd.Stdout, cmd.Stderr, cmd.Stdin = os.Stdout, os.Stderr, os.Stdin
	} else {
		cmd.Stdout, cmd.Stderr = &stdout, &stderr
	}
	if err := cmd.Run(); err != nil {
		detail := strings.TrimSpace(stderr.String())
		if ctx.Err() != nil {
			detail = fmt.Sprintf("no end after %s; %s", c.Timeout, detail)
		}
		return stdout.Bytes(), fmt.Errorf("%s: %w: %s", strings.Join(c.Argv[:min(len(c.Argv), 3)], " "), err, detail)
	}
	return stdout.Bytes(), nil
}

// RealClock is the wall clock.
type RealClock struct{}

// Now implements Clock.
func (RealClock) Now() time.Time { return time.Now() }

// Sleep implements Clock.
func (RealClock) Sleep(d time.Duration) { time.Sleep(d) }

// OSFiles reads and writes the real file system below Root.
type OSFiles struct{ Root string }

// Glob implements Files.
func (f OSFiles) Glob(pattern string) ([]string, error) {
	matches, err := filepath.Glob(filepath.Join(f.Root, pattern))
	if err != nil {
		return nil, err
	}
	rel := make([]string, 0, len(matches))
	for _, m := range matches {
		r, err := filepath.Rel(f.Root, m)
		if err != nil {
			return nil, err
		}
		rel = append(rel, filepath.ToSlash(r))
	}
	return rel, nil
}

// Read implements Files.
func (f OSFiles) Read(path string) ([]byte, error) { return os.ReadFile(filepath.Join(f.Root, path)) }

// ReadHome implements Files.
func (f OSFiles) ReadHome(path string) ([]byte, error) {
	if path == "~" || strings.HasPrefix(path, "~/") {
		home, err := os.UserHomeDir()
		if err != nil {
			return nil, err
		}
		path = filepath.Join(home, strings.TrimPrefix(path, "~"))
	}
	return os.ReadFile(path)
}

// Exists implements Files.
func (f OSFiles) Exists(path string) bool {
	_, err := os.Stat(filepath.Join(f.Root, path))
	return err == nil
}

// Write implements Files, creating parent directories and replacing the file atomically.
func (f OSFiles) Write(path string, data []byte) error {
	full := filepath.Join(f.Root, path)
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		return err
	}
	tmp := full + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, full)
}
