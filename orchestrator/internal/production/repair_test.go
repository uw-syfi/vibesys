package production

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// removeFrom takes files out of a release on disk and returns a function that puts them back, as
// an operator repairing the install would.
func removeFrom(t *testing.T, dir string, files ...string) (repair func()) {
	t.Helper()
	saved := map[string][]byte{}
	for _, f := range files {
		data, err := os.ReadFile(filepath.Join(dir, f))
		if err != nil {
			t.Fatal(err)
		}
		saved[f] = data
		if err := os.Remove(filepath.Join(dir, f)); err != nil {
			t.Fatal(err)
		}
	}
	return func() {
		for f, data := range saved {
			if err := os.WriteFile(filepath.Join(dir, f), data, 0o755); err != nil {
				t.Fatal(err)
			}
		}
	}
}

// Parts are resolved on every start check and every launch. A part missing at one is found by the
// next, once the install is repaired, in the same process: nothing remembers the refusal.
func TestARepairedInstallIsUsedByTheNextStartCheckAndLaunch(t *testing.T) {
	dir, find := diskRelease(t, good)
	repair := removeFrom(t, dir, "bin/vibesys-cli", "sidecar/.venv/bin/python")
	parts := Parts{Find: find, Files: OSFiles{}}
	checks := StartChecks{Sidecars: readyLauncher{}, Parts: parts, Tree: everythingIsADir{}}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	sidecars := &ProcessSidecars{Root: t.TempDir(), Parts: parts, Holders: &ProcessHolders{Log: log}, Log: log}
	launch := func() error {
		_, err := sidecars.Launch(context.Background(), daemon.SessionStart{Launch: core.LaunchSession{Session: core.Session{Agent: "a", N: 1}}})
		return err
	}

	_, err := checks.CheckStart("/repo", "demo")
	if r := refusal(t, err); !r.NotReady || !strings.Contains(r.Reason, "bin/vibesys-cli") {
		t.Fatalf("start check: %+v, want a NotReady refusal naming the missing tool", r)
	}
	if err := launch(); err == nil || !strings.Contains(err.Error(), "sidecar/.venv/bin/python") {
		t.Fatalf("launch: %v, want the missing session holder named", err)
	}

	repair()
	if _, err := checks.CheckStart("/repo", "demo"); err != nil {
		t.Fatalf("start check after the repair: %v", err)
	}
	// The launch now gets past the parts. What it does next (it starts the stand-in program the
	// release holds) is not this test's business, only that the part is no longer the reason.
	if err := launch(); err != nil && strings.Contains(err.Error(), "is incomplete") {
		t.Fatalf("launch after the repair is still refused for a part: %v", err)
	}
}

// The readiness of every part is typed, and Check agrees with it.
func TestReadinessSaysWhyEachPartIsOrIsNotReady(t *testing.T) {
	dir, find := diskRelease(t, good)
	parts := Parts{Find: find, Files: OSFiles{}, Runner: speaks{protocol: RequiredJobToolProtocol}}
	if err := parts.Check(); err != nil {
		t.Fatalf("a whole release is not ready: %v", err)
	}
	for _, r := range parts.Readiness(context.Background()) {
		if r.GetReady().GetOrigin() != pb.PartOrigin_PART_ORIGIN_RELEASE {
			t.Errorf("%v: %v, want ready from the release", r.GetPart(), r)
		}
	}

	removeFrom(t, dir, "bin/vibesys-cli")
	got := map[pb.HostPart]*pb.PartReadiness{}
	for _, r := range parts.Readiness(context.Background()) {
		got[r.GetPart()] = r
	}
	job := got[pb.HostPart_HOST_PART_JOB_TOOL].GetProblem()
	if job.GetReason() != pb.PartProblemReason_PART_PROBLEM_REASON_RELEASE_INCOMPLETE || !strings.Contains(job.GetDetail(), "bin/vibesys-cli") {
		t.Errorf("job tool: %v", got[pb.HostPart_HOST_PART_JOB_TOOL])
	}
	if got[pb.HostPart_HOST_PART_SESSION_HOLDER].GetReady() == nil || got[pb.HostPart_HOST_PART_SKILLS].GetReady() == nil {
		t.Errorf("the parts that are there are not ready: %v", got)
	}
	if err := parts.Check(); err == nil || !strings.Contains(err.Error(), "bin/vibesys-cli") {
		t.Errorf("Check = %v, want the missing tool named", err)
	}
}
