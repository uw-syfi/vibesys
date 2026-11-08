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

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

var mi300x = core.TaskCard{
	Task: "task", Domain: core.DomainLLMServing, Heading: "Serve a model",
	Hardware: &core.AcceleratorNeed{Vendor: core.VendorAMD, Model: core.ModelMI300X, Count: 8}, HardwareName: "AMD MI300X",
}

// placeGiving is a place with a known state that gives eight MI300X when gives is set and no accelerator
// otherwise; unreadable is one whose hardware could not be read. Whether either fits is internal/fit's
// answer, which these tests do not restate: they set up places the rule plainly separates.
func placeGiving(name string, gives bool) *pb.EvalPlace {
	p := &pb.EvalPlace{Name: name, Kind: pb.EvalPlaceKind_EVAL_PLACE_KIND_LOCAL, State: &pb.EvalPlace_Known{Known: &pb.OffersKnown{Source: pb.OffersSource_OFFERS_SOURCE_PROBED}}}
	if gives {
		p.Offers = []*pb.EvalOffer{{Vendor: cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD, Model: cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI300X, Count: 8}}
	}
	return p
}

func unreadable(name string) *pb.EvalPlace {
	return &pb.EvalPlace{Name: name, Kind: pb.EvalPlaceKind_EVAL_PLACE_KIND_LOCAL, State: &pb.EvalPlace_Unreadable{Unreadable: &pb.OffersUnreadable{Reason: pb.OffersUnreadableReason_OFFERS_UNREADABLE_REASON_UNREACHABLE, Detail: "ssh refused"}}}
}

func startAt(id, evalPlace string) *pb.Command {
	cmd := startRun(id)
	cmd.GetStartRun().EvalPlace = evalPlace
	return cmd
}

// rigWith is a rig whose task reads as card and whose host has the places that give eight MI300X, those
// that give nothing and an unreadable one.
func rigWith(t *testing.T, card core.TaskCard, giving []string, others ...string) *daemontest.Rig {
	t.Helper()
	r := daemontest.NewRig(t)
	r.Tasks.Set("task", card)
	var places []*pb.EvalPlace
	for _, name := range giving {
		places = append(places, placeGiving(name, true))
	}
	for _, name := range others {
		places = append(places, placeGiving(name, false))
	}
	r.Places.Set(places...)
	return r
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

// A task the job tool cannot run is refused at a start whatever its place, with the reason the job tool
// gave, as one whose files are invalid is: no run and no receipt, so the same command can start once the
// task is one the tool runs. The reader may say so as an unreadable task or in the card itself, and a
// card that says so is never recorded, even where the place would fit it.
func TestAStartIsRefusedForATaskTheJobToolCannotRun(t *testing.T) {
	why := "vibesys.input.toml: [evaluator] tasks are not supported by this build"
	unsupported := mi300x
	unsupported.Unsupported = why
	for name, set := range map[string]func(*daemontest.Rig){
		"an invalid task": func(r *daemontest.Rig) {
			r.Tasks.Fail("task", &daemon.TaskUnreadable{Reason: daemon.TaskInvalid, Detail: why})
		},
		"an unsupported task": func(r *daemontest.Rig) {
			r.Tasks.Fail("task", &daemon.TaskUnreadable{Reason: daemon.TaskUnsupported, Detail: why})
		},
		"a card that says so": func(r *daemontest.Rig) { r.Tasks.Set("task", unsupported) },
	} {
		t.Run(name, func(t *testing.T) {
			r := rigWith(t, mi300x, []string{"cluster-a"})
			set(r)
			for _, place := range []string{"", "cluster-a", "nowhere"} {
				_, err := r.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: startAt("c1", place)})
				if status.Code(err) != codes.InvalidArgument || !strings.Contains(err.Error(), why) {
					t.Fatalf("place %q: got %v, want InvalidArgument with the reason", place, err)
				}
			}
			if n := len(r.Daemon.State().Runs()); n != 0 {
				t.Fatalf("%d runs, want none", n)
			}
		})
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
			r.Places.Set(placeGiving("cpu-box", true))
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

// A task that needs no accelerator and names no place is left unpinned, and a CPU need is no
// accelerator: neither takes one of the places, however many there are.
func TestATaskThatNeedsNoAcceleratorIsUnpinned(t *testing.T) {
	cpu := core.TaskCard{Task: "task", Hardware: &core.AcceleratorNeed{Vendor: core.VendorCPU, Count: 4}}
	for name, card := range map[string]core.TaskCard{"no hardware": {Task: "task"}, "cpu": cpu} {
		t.Run(name, func(t *testing.T) {
			r := rigWith(t, card, []string{"cluster-a", "cluster-b"}, "cpu-box")
			receipt := dispatch(t, r, startRun("c1"))
			if got := runOf(t, r, receipt); receipt.GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED || got.EvalPlace != "" {
				t.Fatalf("receipt %v place %q, want an accepted start, unpinned", receipt, got.EvalPlace)
			}
		})
	}
}

// A place whose hardware could not be read fits nothing, so a task that needs an accelerator is refused
// it and is not offered it, however it is named.
func TestAPlaceWhoseHardwareIsUnreadableIsNeverRecorded(t *testing.T) {
	r := daemontest.NewRig(t)
	r.Tasks.Set("task", mi300x)
	r.Places.Set(unreadable("cluster-a"))
	for _, named := range []string{"cluster-a", ""} {
		receipt := dispatch(t, r, startAt("c-"+named, named))
		if receipt.GetRefusal() != pb.Refusal_REFUSAL_EVAL_PLACE_MISFIT || len(receipt.GetEvalPlaces()) != 0 {
			t.Fatalf("named %q: receipt %v, want a misfit naming no place", named, receipt)
		}
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
