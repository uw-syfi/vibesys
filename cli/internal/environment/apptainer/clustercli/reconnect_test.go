package clustercli_test

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/clustercli"
)

// fakeSqueue lists one job of the scope d1.
const fakeSqueue = `#!/bin/sh
echo 'vibesys-apptainer-vibesys-cli-01|d1|11|RUNNING|5:00'
`

// attempts is how many times in all a call that never reaches the login node is made.
const attempts = 5

// reconnecting is a cluster on sh whose waits between attempts end at once.
func reconnecting(t *testing.T, sh shell, runtime apptainer.Runtime) *clustercli.Cluster {
	t.Helper()
	if err := os.WriteFile(filepath.Join(sh.bin, "squeue"), []byte(fakeSqueue), 0o755); err != nil {
		t.Fatal(err)
	}
	c, err := clustercli.New(clustercli.Config{Shell: sh, Timers: clock.Immediate{Longest: time.Minute}, Submit: []string{"sbatch"}, Runtime: runtime})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

// Regression: a login node that closed one ssh connection during its key exchange failed a whole
// job, because listing the Slurm jobs was tried once. A call whose connection fails before its
// program starts is made again, up to its attempts, and answers as soon as one connects.
func TestAListingThatDoesNotReachTheLoginNodeIsMadeAgain(t *testing.T) {
	for drops := range attempts + 2 {
		sh := newShell(t)
		sh.lost255 = true
		sh.drops.Store(int32(drops))
		jobs, err := reconnecting(t, sh, apptainer.RuntimeApptainer).Jobs(ctx(t), "d1")
		want := min(drops+1, attempts)
		if got := int(sh.calls.Load()); got != want {
			t.Errorf("%d drops: %d calls, want %d", drops, got, want)
		}
		if drops < attempts {
			if err != nil || len(jobs) != 1 || jobs[0].ID != "11" {
				t.Errorf("%d drops: %+v, %v; want the one job", drops, jobs, err)
			}
			continue
		}
		if err == nil || !strings.Contains(err.Error(), "kex_exchange_identification") || !strings.Contains(err.Error(), "5 attempts") {
			t.Errorf("%d drops: %v; want the last failure and the attempts made", drops, err)
		}
	}
}

// A call that reached the login node is made once, whatever it ended with: a program that failed,
// a connection lost once the session was open, when the command may have run, and a 255 that is
// not the transport's.
func TestACallThatReachedTheLoginNodeIsMadeOnce(t *testing.T) {
	for _, c := range []struct {
		name    string
		lost255 bool
		dropped string
	}{
		{"the program failed", true, ""},
		{"closed in the session", true, "Connection to host-a closed by remote host.\r\n"},
		{"the client loop failed", true, "client_loop: send disconnect: Broken pipe\r\n"},
		{"the server stopped answering", true, "Timeout, server host-a not responding.\r\n"},
		{"a program's own 255", false, kexDropped},
	} {
		sh := newShell(t)
		sh.lost255 = c.lost255
		if c.dropped == "" {
			sh.exit = "3"
		} else {
			sh.dropped = c.dropped
			sh.drops.Store(1)
		}
		if _, err := reconnecting(t, sh, apptainer.RuntimeApptainer).Jobs(ctx(t), "d1"); err == nil {
			t.Errorf("%s: no error", c.name)
		}
		if got := sh.calls.Load(); got != 1 {
			t.Errorf("%s: %d calls, want 1", c.name, got)
		}
	}
}

// A claim of a reusable allocation whose step does not reach the login node is started again, and
// holds the allocation once one does.
func TestAClaimThatDoesNotReachTheLoginNodeIsMadeAgain(t *testing.T) {
	sh := newShell(t)
	sh.lost255 = true
	if err := os.WriteFile(filepath.Join(sh.bin, "node_root"), []byte(t.TempDir()), 0o644); err != nil {
		t.Fatal(err)
	}
	sh.drops.Store(2)
	n := reconnecting(t, sh, apptainer.RuntimePyxis).Node(poolJob(t))
	claim, err := n.Claim(ctx(t))
	if err != nil {
		t.Fatalf("the claim: %v", err)
	}
	if _, err := n.Claim(ctx(t)); !errors.Is(err, apptainer.ErrTaken) {
		t.Errorf("a second claim: %v, want taken", err)
	}
	if err := claim.Close(); err != nil {
		t.Errorf("the release: %v", err)
	}
	if got := sh.calls.Load(); got != 4 {
		t.Errorf("%d calls, want two dropped, the claim, and the second claim", got)
	}
}
