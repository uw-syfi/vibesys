package production_test

import (
	"context"
	"errors"
	"os"
	"reflect"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/production"
)

func TestPlanSharesTaskUntilLastRunEnds(t *testing.T) {
	a := core.Run{ID: "a", Repository: "/repo", Task: "task", Status: core.RunRunning}
	b := a
	b.ID = "b"
	start, stop := production.Plan([]core.Run{a, b}, nil)
	if len(start) != 1 || len(stop) != 0 {
		t.Fatal(start, stop)
	}
	a.Status = core.RunEnded
	next, stop := production.Plan([]core.Run{a, b}, start)
	if len(next) != 0 || len(stop) != 0 {
		t.Fatal(next, stop)
	}
	b.Status = core.RunFailed
	next, stop = production.Plan([]core.Run{a, b}, start)
	if len(next) != 0 || !reflect.DeepEqual(stop, start) {
		t.Fatal(next, stop)
	}
}

type runner struct {
	err       error
	program   string
	args, env []string
}

// answer is what the tool prints when a daemon answers.
const answer = `{"socket":"/run/agent/vibesys-cli/daemon.sock","versions":{"binaryVersion":"1"},"runtimeDirectory":"/run/agent/vibesys-cli"}`

func (r *runner) Run(_ context.Context, p string, a, e []string) ([]byte, []byte, error) {
	r.program = p
	r.args = a
	r.env = e
	return []byte(answer), nil, r.err
}
func TestEnsureUsesRecordedPinnedArgvAndTheRecordedPlace(t *testing.T) {
	r := new(runner)
	e := production.ExecEnsurer{
		Endpoint: core.JobToolEndpoint{Executable: "/opt/custom/vibesys-cli", RuntimeDirectory: "/run/agent/vibesys-cli"}, Runner: r,
		Environ: func() []string { return []string{"HOME=/home/operator", "VIBESYS_CLI_RUNTIME_DIR=/inherited"} },
	}
	ctx, cancel := context.WithDeadline(context.Background(), time.Unix(1<<40, 0))
	defer cancel()
	socket, err := e.Ensure(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if socket.Path() != "/run/agent/vibesys-cli/daemon.sock" {
		t.Fatal(socket)
	}
	if r.program != "/opt/custom/vibesys-cli" || !reflect.DeepEqual(r.args, []string{"daemon", "ensure", "--json"}) {
		t.Fatal(r)
	}
	if !reflect.DeepEqual(r.env, []string{"HOME=/home/operator", "VIBESYS_CLI_RUNTIME_DIR=/run/agent/vibesys-cli"}) {
		t.Fatal(r.env)
	}
}

func FuzzPlanStartsExactlyMissingKeys(f *testing.F) {
	f.Add([]byte{1, 2, 2, 3, 4, 5})
	f.Fuzz(func(t *testing.T, data []byte) {
		var live []core.Run
		var followed []production.Subscription
		want := map[production.Subscription]bool{}
		have := map[production.Subscription]bool{}
		for i, v := range data {
			r := core.Run{Repository: "/repo", Task: string(rune('a' + v%10)), Status: core.RunRunning}
			r.JobTool = core.JobToolEndpoint{Executable: []string{"/a/cli", "/b/cli"}[v%2]}
			k := production.Subscription{JobTool: r.JobTool, Repository: r.Repository, Task: r.Task}
			if i%2 == 0 {
				live = append(live, r)
				want[k] = true
			} else {
				followed = append(followed, k)
				have[k] = true
			}
		}
		start, stop := production.Plan(live, followed)
		for _, k := range start {
			if !want[k] || have[k] {
				t.Fatal("invalid start", k)
			}
			have[k] = true
		}
		for _, k := range stop {
			if want[k] || !have[k] {
				t.Fatal("invalid stop", k)
			}
			delete(have, k)
		}
		if !reflect.DeepEqual(have, want) {
			t.Fatal(have, want)
		}
	})
}

func TestEnsureReportsMissingBinaryAndStartFailure(t *testing.T) {
	ctx, cancel := context.WithDeadline(context.Background(), time.Unix(1<<40, 0))
	defer cancel()
	for _, tc := range []struct {
		err  error
		want core.ViewReason
	}{{os.ErrNotExist, core.CLINotInstalled}, {errors.New("exit 4"), core.StartFailed}} {
		e := production.ExecEnsurerFor(core.JobToolEndpoint{Executable: "/opt/custom/vibesys-cli"}, &runner{err: tc.err})
		_, err := clidaemon.Ensure(ctx, e, core.NoDaemon)
		var unreadable *clidaemon.Unreadable
		if !errors.As(err, &unreadable) || unreadable.Reason != tc.want {
			t.Fatalf("%v", err)
		}
	}
}

func TestEnsureRejectsEmptyAndRelativeCLI(t *testing.T) {
	ctx, cancel := context.WithDeadline(context.Background(), time.Unix(1<<40, 0))
	defer cancel()
	for _, path := range []string{"", "vibesys-cli", "./vibesys-cli"} {
		r := new(runner)
		_, err := production.ExecEnsurerFor(core.JobToolEndpoint{Executable: path}, r).Ensure(ctx)
		var u *clidaemon.Unreadable
		if !errors.As(err, &u) || u.Reason != core.CLINotInstalled || r.program != "" {
			t.Fatalf("path %q: %v, runner %+v", path, err, r)
		}
	}
}
