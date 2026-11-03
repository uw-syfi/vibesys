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

func (r *runner) Run(_ context.Context, p string, a, e []string) error {
	r.program = p
	r.args = a
	r.env = e
	return r.err
}
func TestEnsureUsesSiblingPinnedArgvAndRecordedEnvironment(t *testing.T) {
	r := new(runner)
	e := production.ExecEnsurer{Executable: "/opt/app/vibesysd", Runner: r}
	ctx, cancel := context.WithDeadline(context.Background(), time.Unix(1<<40, 0))
	defer cancel()
	if err := e.Ensure(ctx, clidaemon.Location{Home: "/home/agent", XDGStateHome: "/state", XDGRuntimeDir: "/run/agent"}); err != nil {
		t.Fatal(err)
	}
	if r.program != "/opt/app/vibesys-cli" || !reflect.DeepEqual(r.args, []string{"daemon", "ensure"}) {
		t.Fatal(r)
	}
	want := map[string]bool{"HOME=/home/agent": false, "XDG_STATE_HOME=/state": false, "XDG_RUNTIME_DIR=/run/agent": false}
	for _, v := range r.env {
		if _, ok := want[v]; ok {
			want[v] = true
		}
	}
	for k, v := range want {
		if !v {
			t.Fatalf("missing %s", k)
		}
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
			k := production.Subscription{Repository: r.Repository, Task: r.Task}
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
		want clidaemon.Reason
	}{{os.ErrNotExist, clidaemon.CLINotInstalled}, {errors.New("exit 4"), clidaemon.StartFailed}} {
		e := production.ExecEnsurer{Executable: "/opt/app/vibesysd", Runner: &runner{err: tc.err}}
		err := clidaemon.Ensure(ctx, e, clidaemon.Location{}, clidaemon.NoDaemon)
		var unreadable *clidaemon.Unreadable
		if !errors.As(err, &unreadable) || unreadable.Reason != tc.want {
			t.Fatalf("%v", err)
		}
	}
}
