package production

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// A process id the system has given to another program after the sidecar exited must never be
// taken for the sidecar, or ended as one. This test process stands for that other program: it is
// alive, and its command line does not name the session's directory.
func TestAnotherProgramWithTheSidecarsOldProcessIDIsNotTheSidecar(t *testing.T) {
	root := t.TempDir()
	sidecars := &ProcessSidecars{Root: root, Log: slog.New(slog.DiscardHandler)}
	session := core.Session{Agent: "agent-1", N: 1}
	dir := sidecars.sessionDir(session)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "sidecar.pid"), fmt.Appendf(nil, "%d\n", os.Getpid()), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := sidecars.Attach(context.Background(), session); !errors.Is(err, daemon.ErrSidecarGone) {
		t.Fatalf("attaching to a pid that is another program gave %v, want ErrSidecarGone", err)
	}
}

// A command line is a sidecar's only when it carries the session's directory as a whole argument,
// for any directories: one directory that is a prefix or a part of another does not claim it.
func TestACommandLineBelongsToTheSidecarOfExactlyItsDirectory(t *testing.T) {
	dirs := []string{"/s/a/1", "/s/a/10", "/s/a/1/x", "/s/ab/1", "/s/a", "/s/a/1 ", "a/1"}
	for _, owner := range dirs {
		cmdline := []byte(strings.Join([]string{"python", "-m", "vs_sidecar", "--state-dir", owner, "--socket", owner + "/sock", "--detach"}, "\x00") + "\x00")
		for _, asked := range dirs {
			if got, want := commandOwns(cmdline, asked), asked == owner; got != want {
				t.Errorf("command line of %q owned by %q: %v, want %v", owner, asked, got, want)
			}
		}
	}
}
