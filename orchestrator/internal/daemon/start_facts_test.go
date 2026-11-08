package daemon_test

import (
	"context"
	"errors"
	"reflect"
	"slices"
	"strings"
	"testing"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

var mi300x = core.TaskCard{
	Task: "task", Domain: core.DomainLLMServing, Heading: "Serve a model",
	Hardware: &core.AcceleratorNeed{Vendor: core.VendorAMD, Model: core.ModelMI300X, Count: 8}, HardwareName: "AMD MI300X",
}

func place(name string) *pb.EvalPlace {
	return &pb.EvalPlace{Name: name, Kind: pb.EvalPlaceKind_EVAL_PLACE_KIND_LOCAL, State: &pb.EvalPlace_Known{Known: &pb.OffersKnown{}}}
}

func startAt(id, evalPlace string) *pb.Command {
	cmd := startRun(id)
	cmd.GetStartRun().EvalPlace = evalPlace
	return cmd
}

// rigWith is a rig whose task needs eight MI300X and whose host has the named places, the fitting ones
// as the fit port says.
func rigWith(t *testing.T, card core.TaskCard, fitting []string, others ...string) *daemontest.Rig {
	t.Helper()
	r := daemontest.NewRig(t)
	r.Tasks.Set("task", card)
	for _, name := range append(slices.Clone(fitting), others...) {
		r.Places.Set(append(placesOf(r), place(name))...)
	}
	for _, name := range fitting {
		r.Fit.Set(name, true)
	}
	return r
}

func placesOf(r *daemontest.Rig) []*pb.EvalPlace {
	got, _ := r.Places.Places(context.Background(), false)
	return got
}

func runOf(t *testing.T, r *daemontest.Rig, receipt *pb.Receipt) core.Run {
	t.Helper()
	run, ok := r.Daemon.State().Run(core.RunID(receipt.GetRunId()))
	if !ok {
		t.Fatalf("receipt %v names no run", receipt)
	}
	return run
}

// A start records the task's card as the reader read it from the repository the run is in, once, and
// the record outlives a restart of the daemon.
func TestAStartRecordsTheCardOfItsTaskAndItSurvivesARestart(t *testing.T) {
	r := rigWith(t, core.TaskCard{Task: "task", Heading: "H", Objectives: []core.CardObjective{{Name: "p99", Direction: core.Minimize, Unit: "ms"}}}, nil)
	receipt := dispatch(t, r, startRun("c1"))
	r.Settle(t)
	want := core.TaskCard{Task: "task", Heading: "H", Objectives: []core.CardObjective{{Name: "p99", Direction: core.Minimize, Unit: "ms"}}}
	if got := runOf(t, r, receipt); !reflect.DeepEqual(got.TaskCard, &want) {
		t.Fatalf("card %+v, want %+v", got.TaskCard, want)
	}
	calls := r.Tasks.Calls()
	if len(calls) != 1 || calls[0] != (daemon.TaskRef{Source: daemon.TaskCommitted, Repository: "/repo", Task: "task"}) {
		t.Fatalf("reads %v, want one committed read of the task in the start's repository", calls)
	}
	r.Restart(t)
	if got := runOf(t, r, receipt); !reflect.DeepEqual(got.TaskCard, &want) {
		t.Fatalf("after a restart the card is %+v, want %+v", got.TaskCard, want)
	}
}

// A task the reader cannot find gives a run with no card, as an explicit state, never a guessed card.
// A host with no reader at all is the same.
func TestATaskWithNoCardStartsARunThatRecordsNone(t *testing.T) {
	r := daemontest.NewRig(t)
	receipt := dispatch(t, r, startRun("c1"))
	if got := runOf(t, r, receipt); receipt.GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED || got.TaskCard != nil {
		t.Fatalf("receipt %v card %+v, want an accepted start with no card", receipt, got.TaskCard)
	}
}

// A task whose files are invalid is refused with the reader's words naming the key, and a reader that
// fails is a failure to retry; neither makes a run or a receipt, so the same command can start later.
func TestAnUnreadableTaskRefusesTheStartAndALostReadIsRetried(t *testing.T) {
	r := daemontest.NewRig(t)
	r.Tasks.Fail("task", &daemon.TaskUnreadable{Reason: daemon.TaskInvalid, Detail: "objectives.toml: unknown key \"metrik\""})
	_, err := r.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: startRun("c1")})
	if status.Code(err) != codes.InvalidArgument || !strings.Contains(err.Error(), `unknown key "metrik"`) {
		t.Fatalf("got %v, want InvalidArgument naming the key", err)
	}
	r.Tasks.Fail("task", errors.New("the daemon is gone"))
	if _, err := r.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: startRun("c1")}); status.Code(err) != codes.Unavailable {
		t.Fatalf("got %v, want Unavailable", err)
	}
	if n := len(r.Daemon.State().Runs()); n != 0 {
		t.Fatalf("%d runs, want none", n)
	}
	r.Tasks.Fail("task", nil)
	r.Tasks.Set("task", core.TaskCard{Task: "task"})
	if receipt := dispatch(t, r, startRun("c1")); receipt.GetRunId() == "" {
		t.Fatalf("receipt %v, want a run once the task reads", receipt)
	}
}

// A place that does not give what the task needs is refused with its own refusal, the fitting places
// named, and no run. A named place the host lacks, and an unnamed choice among several, likewise.
func TestAStartIsRefusedForAPlaceThatDoesNotFitOrIsNotThereOrIsAmbiguous(t *testing.T) {
	cases := []struct {
		name  string
		place string
		want  pb.Refusal
		named []string
	}{
		{"misfit", "cpu-box", pb.Refusal_REFUSAL_EVAL_PLACE_MISFIT, []string{"cluster-a", "cluster-b"}},
		{"not found", "nowhere", pb.Refusal_REFUSAL_EVAL_PLACE_NOT_FOUND, []string{"cluster-a", "cluster-b", "cpu-box"}},
		{"ambiguous", "", pb.Refusal_REFUSAL_EVAL_PLACE_AMBIGUOUS, []string{"cluster-a", "cluster-b"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r := rigWith(t, mi300x, []string{"cluster-b", "cluster-a"}, "cpu-box")
			receipt := dispatch(t, r, startAt("c1", c.place))
			if receipt.GetRefusal() != c.want || !slices.Equal(receipt.GetEvalPlaces(), c.named) || receipt.GetRunId() != "" {
				t.Fatalf("receipt %v, want %v naming %v and no run", receipt, c.want, c.named)
			}
			if n := len(r.Daemon.State().Runs()); n != 0 {
				t.Fatalf("%d runs, want none", n)
			}
			// The refusal is the command's receipt: a retry answers it again, even once the places change.
			r.Fit.Set("cpu-box", true)
			if again := dispatch(t, r, startAt("c1", c.place)); again.GetRefusal() != c.want {
				t.Fatalf("retry %v, want the same refusal", again)
			}
		})
	}
}

// A task that needs an accelerator takes the only place that fits when none is named, and the place
// it names when that fits. The run records the resolved place, and it is the one place a start
// ever records.
func TestAStartRecordsTheResolvedEvalPlace(t *testing.T) {
	for _, c := range []struct{ name, named, want string }{{"resolved", "", "cluster-a"}, {"named", "cluster-a", "cluster-a"}} {
		t.Run(c.name, func(t *testing.T) {
			r := rigWith(t, mi300x, []string{"cluster-a"}, "cpu-box")
			receipt := dispatch(t, r, startAt("c1", c.named))
			if receipt.GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
				t.Fatalf("receipt %v, want accepted", receipt)
			}
			r.Settle(t)
			if got := runOf(t, r, receipt); got.EvalPlace != c.want || got.TaskCard == nil || got.TaskCard.Hardware == nil {
				t.Fatalf("run place %q card %+v, want place %q and the card", got.EvalPlace, got.TaskCard, c.want)
			}
			r.Restart(t)
			if got := runOf(t, r, receipt); got.EvalPlace != c.want {
				t.Fatalf("after a restart the place is %q", got.EvalPlace)
			}
		})
	}
}

// Fit is the port's alone, and it is asked only of a task that needs something: a task with no
// accelerator and no place named never reads the places, and is left unpinned.
func TestATaskThatNeedsNoAcceleratorIsUnpinnedAndFitIsNeverAsked(t *testing.T) {
	r := rigWith(t, core.TaskCard{Task: "task"}, []string{"cluster-a"}, "cluster-b")
	receipt := dispatch(t, r, startRun("c1"))
	if got := runOf(t, r, receipt); got.EvalPlace != "" || len(r.Fit.Asked()) != 0 {
		t.Fatalf("place %q, fit asked %v, want unpinned and no question", got.EvalPlace, r.Fit.Asked())
	}
}

// A failure to list the places is a failure of the call to retry, not a verdict on the place.
func TestAFailureToListPlacesIsRetriedNotRefused(t *testing.T) {
	r := rigWith(t, mi300x, []string{"cluster-a"})
	r.Places.Fail(errors.New("probe failed"))
	_, err := r.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: startAt("c1", "cluster-a")})
	if status.Code(err) != codes.Unavailable {
		t.Fatalf("got %v, want Unavailable", err)
	}
	r.Places.Fail(nil)
	if receipt := dispatch(t, r, startAt("c1", "cluster-a")); receipt.GetRunId() == "" {
		t.Fatalf("receipt %v, want a run", receipt)
	}
}
