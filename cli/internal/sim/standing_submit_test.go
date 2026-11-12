package sim_test

import (
	"context"
	"fmt"
	"google.golang.org/protobuf/types/known/durationpb"
	"slices"
	"strings"
	"testing"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// A job submitted into a standing allocation runs in it, whatever kind of job it is and however it
// is asked for. These tests submit through the daemon's own service (daemon.Server.Submit, the call
// the command line makes), a task of two images on an apptainer placement, and an allocation that
// is short (20 minutes) beside the 30 minutes the daemon once took every check and measure to need.

// twoImageSource is a task of schema 0.2, the shape of a task whose candidate is served beside the
// task's own image, with timeouts of a minute.
func twoImageSource(check, bench []string) task.Tree {
	quote := func(argv []string) string {
		parts := make([]string, len(argv))
		for i, a := range argv {
			parts[i] = fmt.Sprintf("%q", a)
		}
		return "[" + strings.Join(parts, ", ") + "]"
	}
	dir := task.TasksDir + "/task/"
	manifest := fmt.Sprintf(`
version = "0.2"
backends = ["cpu"]
metric = "ops"

[candidate]
dockerfile = "serve/Dockerfile"
context = "serve"
serve = ["print", "serving on ${PORT}", ";", "ready", ";", "hold", "forever"]
ready = "/health"
startup_timeout_seconds = 120

[accuracy]
command = %s
timeout_seconds = 60

[benchmark]
command = %s
timeout_seconds = 60
result_protocol = 2

[profile]
command = ["print", "profile load", ";", "exit", "0"]
timeout_seconds = 60
`, quote(check), quote(bench))
	return task.Tree{
		dir + task.ManifestName:  {Data: []byte(manifest)},
		dir + task.ObjectiveName: {Data: []byte("[[objective]]\nname = \"ops\"\ndirection = \"max\"\n")},
		dir + "Dockerfile":       {Data: []byte("FROM busybox:1.36.1\n")},
		"serve/Dockerfile":       {Data: []byte("FROM busybox:1.36.1\nCOPY . /srv\n")},
	}
}

const shortAllocation = 20 * time.Minute

type submitKind struct {
	name     string
	settings func() cliv1.JobSpec
}

// submitKinds are the kinds of job a user submits: a check, a measure, and a command in either
// image, the task's one beside the candidate's server under a wrapper (as a profile is taken).
var submitKinds = []submitKind{
	{"check", func() cliv1.JobSpec {
		return cliv1.JobSpec{Settings: &cliv1.JobSpec_Check{Check: &cliv1.CheckSettings{}}}
	}},
	{"measure", func() cliv1.JobSpec {
		return cliv1.JobSpec{Settings: &cliv1.JobSpec_Measure{Measure: &cliv1.MeasureSettings{}}}
	}},
	{"command", func() cliv1.JobSpec {
		return cliv1.JobSpec{Settings: &cliv1.JobSpec_Command{Command: &cliv1.CommandSettings{Image: cliv1.CommandImage_COMMAND_IMAGE_CANDIDATE, Argv: []string{"exit", "0"}, Timeout: durationpb.New(5 * time.Minute)}}}
	}},
	{"harness", func() cliv1.JobSpec {
		return cliv1.JobSpec{Settings: &cliv1.JobSpec_Command{Command: &cliv1.CommandSettings{Image: cliv1.CommandImage_COMMAND_IMAGE_HARNESS, Wrap: []string{"print", "profiler on", ";"}, Argv: []string{"print", "load", ";", "exit", "0"}, Timeout: durationpb.New(5 * time.Minute)}}}
	}},
}

// submitThrough is the command line's submit: the call of the daemon's service, with the run of the
// caller, for the kind of job, into an allocation, on a placement or on any.
func (r *standingRig) submitThrough(srv interface {
	Submit(context.Context, *cliv1.SubmitRequest) (*cliv1.SubmitResponse, error)
}, k submitKind, into, placement string) core.JobID {
	r.t.Helper()
	spec := k.settings()
	spec.Commit, spec.Environment = string(standingCommit), cliv1.EnvironmentKind_ENVIRONMENT_KIND_DOCKER
	spec.Placement, spec.Allocation = placement, into
	resp, err := srv.Submit(r.ctx(), &cliv1.SubmitRequest{
		Context: &cliv1.CallContext{Repository: "/repo", Task: "task", RunId: string(standingCall.Run), Harness: "h", Agent: "agent-1"},
		Spec:    &spec,
		NoPlan:  k.name == "measure",
	})
	if err != nil {
		r.t.Fatalf("submit %s --alloc %s --placement %q: %v", k.name, into, placement, err)
	}
	return core.JobID(resp.GetJob().GetId())
}

func TestEveryKindOfJobSubmittedIntoAStandingAllocationRunsInIt(t *testing.T) {
	for _, k := range submitKinds {
		for _, strict := range []bool{false, true} {
			for _, placement := range []string{"", clusterPlacement} {
				for _, restarted := range []bool{false, true} {
					into := core.AllocationAny
					if strict {
						into = "a1"
					}
					name := fmt.Sprintf("%s/into=%s/placement=%q/restarted=%v", k.name, into, placement, restarted)
					t.Run(name, func(t *testing.T) {
						inStandingBubble(t, func(t *testing.T) {
							r := newStandingRig(t, standingConfig)
							r.tasks.Put("/repo", standingCommit, twoImageSource([]string{"exit", "0"}, protocol2Scores))
							a, srv := r.start()
							first, err := a.RequestAllocation(r.ctx(), standingCall.Run, clusterPlacement, shortAllocation)
							if err != nil {
								t.Fatal(err)
							}
							if first.ID != "a1" {
								t.Fatalf("the first allocation is %s, the test names a1", first.ID)
							}
							standing := r.await(a, first.ID, app.AllocUntil{Kind: app.UntilRunning}).Handle.Ref
							if restarted {
								a.Close()
								a, srv = r.start()
								r.await(a, first.ID, app.AllocUntil{Kind: app.UntilRunning})
							}
							id := r.submitThrough(srv, k, into, placement)
							awaitFinal(t, a, id)
							// The containers of the fake cluster write no result file, so a measure's samples end
							// in failure; where the job ran is what is judged for it.
							if j, _ := a.Job(r.ctx(), standingScope, id); j.State != core.JobSucceeded && k.name != "measure" {
								var log strings.Builder
								_ = a.ReadLogs(r.ctx(), standingScope, id, 0, false, func(c core.LogChunk) error { log.Write(c.Data); return nil })
								t.Fatalf("job %s ended %s: %s", id, j.State, log.String())
							}
							if boxes := r.cluster.StartedIn(standing); len(boxes) == 0 {
								t.Errorf("no process of the job ran in the standing allocation %s", standing)
							}
							if got := r.cluster.Submitted(); len(got) != 1 {
								t.Errorf("%d batch jobs submitted, want the standing allocation alone: %+v", len(got), got)
							}
						})
					})
				}
			}
		}
	}
}

// protocol2Scores is a benchmark that reports ops through the evaluator stream.
var protocol2Scores = []string{"emit", `{"kind":"hello","protocol":2,"metrics":{"ops":{}}}` + "\n" + `{"kind":"result","values":{"ops":7.5}}`}

var _ = slices.Contains[[]string]

// awaitFinal returns once the job is final.
func awaitFinal(t *testing.T, a *app.App, id core.JobID) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	for {
		j, err := a.Job(ctx, standingScope, id)
		if err != nil {
			t.Fatal(err)
		}
		if j.State.Final() {
			return
		}
		if _, err := a.Wait(ctx, standingScope, core.WaitFor{IDs: []core.JobID{id}}, func(core.Job) error { return nil }); err != nil {
			t.Fatal(err)
		}
	}
}

// A job whose own check fails, as one does that finds no GPU where it runs (the container has no
// rocminfo, and the check exits 127), fails as any job does, after it started in the allocation.
// The allocation is not told it is lost, and it takes the next job: such a failure is the job's, not
// the allocation's.
func TestAJobWhoseCheckFailsFailsWithoutHarmingItsAllocation(t *testing.T) {
	inStandingBubble(t, func(t *testing.T) {
		r := newStandingRig(t, standingConfig)
		r.tasks.Put("/repo", standingCommit, twoImageSource([]string{"exit", "127"}, protocol2Scores))
		a, srv := r.start()
		first, err := a.RequestAllocation(r.ctx(), standingCall.Run, clusterPlacement, shortAllocation)
		if err != nil {
			t.Fatal(err)
		}
		standing := r.await(a, first.ID, app.AllocUntil{Kind: app.UntilRunning}).Handle.Ref
		for range 2 {
			id := r.submitThrough(srv, submitKinds[0], core.AllocationAny, "")
			awaitFinal(t, a, id)
			if j, _ := a.Job(r.ctx(), standingScope, id); j.State != core.JobFailed {
				t.Fatalf("job %s ended %s, want failed", id, j.State)
			}
		}
		v, err := a.Allocation(first.ID)
		if err != nil {
			t.Fatal(err)
		}
		if v.Phase.Stage() != alloc.StageRunning || len(r.cluster.Submitted()) != 1 {
			t.Fatalf("allocation %+v after two failed jobs, %d batch jobs", v.Phase, len(r.cluster.Submitted()))
		}
		if n := len(r.cluster.StartedIn(standing)); n < 4 {
			t.Fatalf("%d containers started in the allocation, want both jobs' two", n)
		}
	})
}

// A job submitted into any allocation that none takes runs without one, and says why on its notes
// (what `jobs` and `jobs --json` show) and at the head of its output (what `logs` shows), in the
// words of the refusal that came closest to taking it: the allocation it did not fit, or that the
// run holds none.
func TestAJobOfAnyAllocationThatNoneTakesSaysWhy(t *testing.T) {
	for _, k := range submitKinds {
		for _, held := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/held=%v", k.name, held), func(t *testing.T) {
				inStandingBubble(t, func(t *testing.T) {
					r := newStandingRig(t, standingConfig)
					r.tasks.Put("/repo", standingCommit, twoImageSource([]string{"exit", "0"}, protocol2Scores))
					a, srv := r.start()
					want := "no running allocation"
					if held {
						first, err := a.RequestAllocation(r.ctx(), standingCall.Run, clusterPlacement, 2*time.Minute)
						if err != nil {
							t.Fatal(err)
						}
						r.await(a, first.ID, app.AllocUntil{Kind: app.UntilRunning})
						want = "does not fit allocation a1"
					}
					id := r.submitThrough(srv, k, core.AllocationAny, "")
					awaitFinal(t, a, id)
					j, err := a.Job(r.ctx(), standingScope, id)
					if err != nil {
						t.Fatal(err)
					}
					note := ""
					for _, n := range j.Notes {
						note += n.Text + "\n"
					}
					var log strings.Builder
					_ = a.ReadLogs(r.ctx(), standingScope, id, 0, false, func(c core.LogChunk) error { log.Write(c.Data); return nil })
					for where, text := range map[string]string{"notes": note, "output": log.String()} {
						if !strings.Contains(text, "allocation any: ") || !strings.Contains(text, want) || !strings.Contains(text, "queued without one") {
							t.Errorf("the job's %s %q do not say why it has no allocation (%q)", where, text, want)
						}
					}
				})
			})
		}
	}
}
