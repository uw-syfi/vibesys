package clustercli_test

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/clustercli"
)

// elapsing is timers on which time passes only by the waits between calls: a timeout of up to a
// minute fires at once and moves the time on by its length, and a longer one, a call's bound,
// never fires. So a test says for how long of this time a login node refuses connections, and no
// result depends on how long anything took.
type elapsing struct {
	mu  sync.Mutex
	now time.Duration
}

func (e *elapsing) Timeout(_ context.Context, d time.Duration) (<-chan struct{}, func()) {
	fired := make(chan struct{})
	if d <= time.Minute {
		e.mu.Lock()
		e.now += d
		e.mu.Unlock()
		close(fired)
	}
	return fired, func() {}
}

// before reports whether the time is still before d.
func (e *elapsing) before(d time.Duration) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.now < d
}

// The fakes of Slurm's programs the calls below use: sbatch prints a job id, and squeue lists one
// running job, and none of a name, which a cancel then confirms ended.
var outageFakes = map[string]string{
	"sbatch": "#!/bin/sh\necho 4242\n",
	"squeue": "#!/bin/sh\nfor a do [ \"$a\" = -n ] && exit 0; done\necho 'RUNNING|None|N/A|node-a'\n",
}

// Regression: a login node that refused new ssh connections for a minute or two (sshd's
// MaxStartups: "kex_exchange_identification: Connection closed by remote host", ssh's exit 255)
// failed every job it ran, those waiting in Slurm's queue among them, whose allocations were then
// cancelled: each call that did not reach it was made again for 15 seconds only. Every call a job
// makes of the login node while its allocation waits and while it runs outlasts such an outage,
// and answers once the login node takes connections again.
func TestALoginNodeThatRefusesConnectionsForTwoMinutesFailsNoCall(t *testing.T) {
	const outage = 2 * time.Minute
	calls := map[string]func(t *testing.T, c *clustercli.Cluster, tmp string) error{
		"reading a queued job's state": func(t *testing.T, c *clustercli.Cluster, _ string) error {
			st, err := c.State(ctx(t), "4242")
			if err == nil && st.Phase != apptainer.Running {
				t.Errorf("state %+v, want running", st)
			}
			return err
		},
		"listing the jobs": func(t *testing.T, c *clustercli.Cluster, _ string) error {
			_, err := c.Jobs(ctx(t), "d1")
			return err
		},
		"submitting an allocation": func(t *testing.T, c *clustercli.Cluster, _ string) error {
			id, err := c.Submit(ctx(t), apptainer.Allocation{Name: "vibesys-apptainer-c", Comment: "d1", Limit: time.Hour})
			if err == nil && id != "4242" {
				t.Errorf("job %q", id)
			}
			return err
		},
		"holding the allocation once it runs": func(t *testing.T, c *clustercli.Cluster, _ string) error {
			h, err := c.Node("77").Hold(ctx(t))
			if err != nil {
				return err
			}
			return h.Close()
		},
		"reading the node": func(t *testing.T, c *clustercli.Cluster, _ string) error {
			_, err := c.Node("77").Read(ctx(t), map[string]string{"a": "echo a"})
			return err
		},
		"starting a command's step": func(t *testing.T, c *clustercli.Cluster, _ string) error {
			var out bytes.Buffer
			p, err := c.Node("77").Start(apptainer.Box{Name: "b", Image: "/shared/x.sif"}, []string{"/bin/sh", "-c", "echo hi; exit 3"}, &out)
			if err != nil {
				return err
			}
			if e := wait(t, p); e != (environment.Exit{Code: 3}) || out.String() != "hi\n" {
				t.Errorf("exit %+v, output %q; want the command's", e, out.String())
			}
			return nil
		},
		"signalling a box": func(t *testing.T, c *clustercli.Cluster, _ string) error {
			return c.Node("77").Signal(ctx(t), "b", apptainer.SIGTERM)
		},
		"cancelling an allocation": func(t *testing.T, c *clustercli.Cluster, _ string) error {
			return c.Cancel(ctx(t), "vibesys-apptainer-c")
		},
	}
	for name, call := range calls {
		t.Run(name, func(t *testing.T) {
			sh := newShell(t)
			sh.lost255 = true
			tmp := t.TempDir()
			sh.env = []string{"TMPDIR=" + tmp}
			for prog, text := range outageFakes {
				if err := os.WriteFile(filepath.Join(sh.bin, prog), []byte(text), 0o755); err != nil {
					t.Fatal(err)
				}
			}
			timers := &elapsing{}
			sh.refusing = func() bool { return timers.before(outage) }
			c, err := clustercli.New(clustercli.Config{Shell: sh, Timers: timers, Submit: []string{"sbatch"}})
			if err != nil {
				t.Fatal(err)
			}
			if err := call(t, c, tmp); err != nil {
				t.Fatalf("failed: %v", err)
			}
			if timers.before(outage) {
				t.Errorf("answered while the login node refused connections")
			}
		})
	}
}

// A login node that refuses connections for longer than the calls are made again fails the call,
// saying how many attempts were made, and what ssh said.
func TestALoginNodeThatKeepsRefusingConnectionsFailsTheCall(t *testing.T) {
	sh := newShell(t)
	sh.lost255 = true
	sh.refusing = func() bool { return true }
	timers := &elapsing{}
	c, err := clustercli.New(clustercli.Config{Shell: sh, Timers: timers, Submit: []string{"sbatch"}})
	if err != nil {
		t.Fatal(err)
	}
	_, err = c.State(ctx(t), "4242")
	if err == nil || !strings.Contains(err.Error(), "kex_exchange_identification") || !strings.Contains(err.Error(), "attempts") {
		t.Errorf("%v; want the last failure and the attempts made", err)
	}
	if timers.before(3 * time.Minute) {
		t.Errorf("gave up after %v of refusals", timers.now)
	}
}
