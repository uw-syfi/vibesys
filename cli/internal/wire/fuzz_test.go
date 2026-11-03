package wire_test

import (
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	"google.golang.org/genproto/googleapis/rpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/anypb"
	"google.golang.org/protobuf/types/known/durationpb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
	statuspb "google.golang.org/grpc/status"
)

var failureKinds = []core.FailureKind{
	core.FailureInvalidInput, core.FailureNotFound, core.FailureConflict, core.FailureUnavailable,
	core.FailureIncompatible, core.FailureEnvironmentMismatch, core.FailureNotMeasured, core.FailureInternal,
}

// These fuzz targets feed arbitrary bytes, the shape an untrusted peer sends, to the decoders that
// turn a contract message into a domain value. Each target holds a decoder to three things: it
// never panics, it refuses with an error or returns a value the domain accepts, and a value it
// returned survives being encoded and decoded again unchanged. `go test` runs the seeds; run one
// target with `go test -fuzz=FuzzDecodeJob -fuzztime=1m ./internal/wire`, and commit the files
// the fuzzer leaves under testdata/fuzz when it finds a failure.

// seedMessages adds, for each seed message, its encoding to the corpus, so the fuzzer starts from
// shapes the decoders accept and mutates towards the ones they should refuse.
func seedMessages(f *testing.F, messages ...proto.Message) {
	f.Helper()
	f.Add([]byte(nil))
	for _, m := range messages {
		data, err := proto.Marshal(m)
		if err != nil {
			f.Fatal(err)
		}
		f.Add(data)
	}
}

// decode unmarshals data into m, and skips the input when it is not a message at all, since the
// transport refuses that before any decoder sees it.
func decode(t *testing.T, data []byte, m proto.Message) {
	t.Helper()
	if err := proto.Unmarshal(data, m); err != nil {
		t.Skip()
	}
}

func FuzzDecodeJob(f *testing.F) {
	r := newRand(10)
	var seeds []proto.Message
	for range 12 {
		seeds = append(seeds, wire.Job(anyJob(r)))
	}
	seedMessages(f, seeds...)
	f.Fuzz(func(t *testing.T, data []byte) {
		message := &cliv1.Job{}
		decode(t, data, message)
		first, err := wire.ToJob(message)
		if err != nil {
			return
		}
		again, err := wire.ToJob(wire.Job(first))
		if err != nil {
			t.Fatalf("a decoded job %+v is refused once encoded again: %v", first, err)
		}
		if !reflect.DeepEqual(again, first) {
			t.Fatalf("a decoded job changed when encoded and decoded again:\n got %+v\nwant %+v", again, first)
		}
		if !first.Spec.Commit.Valid() {
			t.Fatalf("a decoded job has the commit %q, which is not a full hash", first.Spec.Commit)
		}
	})
}

func FuzzDecodeWaitRequest(f *testing.F) {
	seedMessages(f,
		&cliv1.WaitRequest{JobIds: []string{"j1"}},
		&cliv1.WaitRequest{JobIds: []string{"j1", "j2"}, Any: true, Timeout: durationpb.New(90 * time.Second)},
		&cliv1.WaitRequest{JobIds: []string{"j1"}, Timeout: &durationpb.Duration{Seconds: -1}},
		&cliv1.WaitRequest{JobIds: []string{""}},
	)
	f.Fuzz(func(t *testing.T, data []byte) {
		message := &cliv1.WaitRequest{}
		decode(t, data, message)
		w, err := wire.ToWaitFor(message)
		if err != nil {
			return
		}
		if len(w.IDs) == 0 || slices.Contains(w.IDs, "") {
			t.Fatalf("a decoded wait %+v names no job or an empty one", w)
		}
		if w.Timeout < 0 || (message.GetTimeout() != nil && w.Timeout == 0) {
			t.Fatalf("a decoded wait has the timeout %v from %v", w.Timeout, message.GetTimeout())
		}
		again := &cliv1.WaitRequest{Any: w.Any}
		for _, id := range w.IDs {
			again.JobIds = append(again.JobIds, string(id))
		}
		if w.Timeout > 0 {
			again.Timeout = durationpb.New(w.Timeout)
		}
		back, err := wire.ToWaitFor(again)
		if err != nil || !reflect.DeepEqual(back, w) {
			t.Fatalf("a decoded wait %+v changed when encoded and decoded again: %+v, %v", w, back, err)
		}
	})
}

func FuzzDecodeJobSpec(f *testing.F) {
	r := newRand(11)
	var seeds []proto.Message
	for range 12 {
		seeds = append(seeds, wire.JobSpec(anyJob(r).Spec))
	}
	seedMessages(f, seeds...)
	f.Fuzz(func(t *testing.T, data []byte) {
		message := &cliv1.JobSpec{}
		decode(t, data, message)
		first, err := wire.ToJobSpec("spec", message)
		if err != nil {
			if _, ok := wire.FailureOf(err); !ok || !wire.IsInvalidInput(err) {
				t.Fatalf("a refused spec gave %v, want an invalid-input failure", err)
			}
			return
		}
		if !first.Commit.Valid() || first.Settings == nil {
			t.Fatalf("a decoded spec %+v has no valid commit or settings", first)
		}
		again, err := wire.ToJobSpec("spec", wire.JobSpec(first))
		if err != nil || !reflect.DeepEqual(again, first) {
			t.Fatalf("a decoded spec changed when encoded and decoded again: %+v, %v; want %+v", again, err, first)
		}
	})
}

func FuzzDecodeLaunch(f *testing.F) {
	r := newRand(12)
	var seeds []proto.Message
	for range 12 {
		seeds = append(seeds, &cliv1.Launch{Repository: "/repo", Task: "kv", JobId: "j1", Spec: wire.JobSpec(anyJob(r).Spec)})
	}
	for range 12 {
		p := anyFlowPlan(r)
		seeds = append(seeds, wire.Launch(launchWith(r, &p)))
	}
	seedMessages(f, seeds...)
	f.Fuzz(func(t *testing.T, data []byte) {
		message := &cliv1.Launch{}
		decode(t, data, message)
		first, err := wire.ToLaunch(message)
		if err != nil {
			return
		}
		if first.Repository == "" || first.Task == "" || first.Job == "" || first.Spec.Environment == core.EnvironmentAuto {
			t.Fatalf("a decoded launch %+v lacks what a supervisor is promised", first)
		}
		if c, ok := first.Spec.Settings.(core.CommandSettings); ok && c.Timeout <= 0 {
			t.Fatalf("a decoded launch has the command timeout %v", c.Timeout)
		}
		again, err := wire.ToLaunch(wire.Launch(first))
		if err != nil || !reflect.DeepEqual(again, first) {
			t.Fatalf("a decoded launch changed when encoded and decoded again: %+v, %v; want %+v", again, err, first)
		}
	})
}

func FuzzDecodeJobFilter(f *testing.F) {
	seedMessages(f,
		&cliv1.ListJobsRequest{},
		&cliv1.ListJobsRequest{Commit: strings.Repeat("a", 40), NoteContains: "x"},
		&cliv1.ListJobsRequest{Commit: "abc", States: []cliv1.JobState{cliv1.JobState_JOB_STATE_QUEUED}},
		&cliv1.ListJobsRequest{States: []cliv1.JobState{99, -1}},
	)
	f.Fuzz(func(t *testing.T, data []byte) {
		message := &cliv1.ListJobsRequest{}
		decode(t, data, message)
		filter, err := wire.ToJobFilter(message)
		if err != nil {
			return
		}
		if filter.Commit != "" && !filter.Commit.Valid() {
			t.Fatalf("a decoded filter has the commit %q, which is not a full hash", filter.Commit)
		}
		if len(filter.States) != len(message.GetStates()) {
			t.Fatalf("a decoded filter has %d states for %d sent", len(filter.States), len(message.GetStates()))
		}
	})
}

func FuzzDecodePlan(f *testing.F) {
	seedMessages(f,
		&cliv1.Plan{},
		&cliv1.Plan{Number: 1, Title: "t", Body: "b", Status: cliv1.PlanStatus_PLAN_STATUS_OPEN},
		&cliv1.Plan{Number: 2, Status: cliv1.PlanStatus_PLAN_STATUS_WON, Comments: []*cliv1.PlanComment{{Text: "c", Agent: "a"}}},
		&cliv1.Plan{Status: 99},
	)
	f.Fuzz(func(t *testing.T, data []byte) {
		message := &cliv1.Plan{}
		decode(t, data, message)
		_, statusErr := wire.ToPlanStatus("status", message.GetStatus())
		first, err := wire.ToPlan(message)
		if (err != nil) != (statusErr != nil) {
			t.Fatalf("a plan with status %s: ToPlan error %v, ToPlanStatus error %v; they must agree", message.GetStatus(), err, statusErr)
		}
		if err != nil {
			return
		}
		if !first.Status.Valid() {
			t.Fatalf("a decoded plan has the status %q, which is not a status", first.Status)
		}
		for i, c := range message.GetComments() {
			if got := wire.ToPlanComment(c); !reflect.DeepEqual(got, first.Comments[i]) {
				t.Fatalf("comment %d decoded alone as %+v, in its plan as %+v", i, got, first.Comments[i])
			}
		}
		again, err := wire.ToPlan(wire.Plan(first))
		if err != nil {
			t.Fatalf("a decoded plan %+v is refused once encoded again: %v", first, err)
		}
		if !reflect.DeepEqual(again, first) {
			t.Fatalf("a decoded plan changed when encoded and decoded again:\n got %+v\nwant %+v", again, first)
		}
	})
}

func FuzzDecodePlanFilter(f *testing.F) {
	seedMessages(f,
		&cliv1.ListPlansRequest{},
		&cliv1.ListPlansRequest{Text: "x", Statuses: []cliv1.PlanStatus{cliv1.PlanStatus_PLAN_STATUS_OPEN, cliv1.PlanStatus_PLAN_STATUS_WON}},
		&cliv1.ListPlansRequest{Statuses: []cliv1.PlanStatus{99, -1, 0}},
	)
	f.Fuzz(func(t *testing.T, data []byte) {
		message := &cliv1.ListPlansRequest{}
		decode(t, data, message)
		filter, err := wire.ToPlanFilter(message)
		if err != nil {
			return
		}
		if len(filter.Statuses) != len(message.GetStatuses()) {
			t.Fatalf("a decoded filter has %d statuses for %d sent", len(filter.Statuses), len(message.GetStatuses()))
		}
		for _, s := range filter.Statuses {
			if !s.Valid() {
				t.Fatalf("a decoded filter has the status %q, which is not a status", s)
			}
		}
	})
}

// A supervisor's outcome and a peer's failure decode with no error to return, so what they must
// hold is that they never panic and that an outcome that does not say the job succeeded is never
// a success: only a check or a measure job's pass with its result, and a command job's command
// that was seen to exit 0, are.
func FuzzDecodeOutcome(f *testing.F) {
	exited := &cliv1.ExecutionOutcome{Kind: cliv1.ExecutionKind_EXECUTION_KIND_EXITED}
	seedMessages(f,
		&cliv1.SupervisorOutcome{},
		&cliv1.SupervisorOutcome{Result: &cliv1.SupervisorOutcome_Executed{Executed: exited}},
		&cliv1.SupervisorOutcome{Result: &cliv1.SupervisorOutcome_NotAttempted{NotAttempted: &cliv1.Failure{Message: "no image"}}},
		&cliv1.SupervisorOutcome{Result: &cliv1.SupervisorOutcome_Command{Command: &cliv1.CommandEnd{Attempt: &cliv1.CommandEnd_Ended{Ended: exited}}}},
		&cliv1.SupervisorOutcome{Result: &cliv1.SupervisorOutcome_Command{Command: &cliv1.CommandEnd{Attempt: &cliv1.CommandEnd_Unobserved{}}}},
		&cliv1.SupervisorOutcome{Result: &cliv1.SupervisorOutcome_Command{Command: &cliv1.CommandEnd{}}},
		&cliv1.SupervisorOutcome{Result: &cliv1.SupervisorOutcome_Flow{Flow: &cliv1.FlowEnd{Verdict: &cliv1.FlowEnd_Stopped{Stopped: exited}}}},
		&cliv1.SupervisorOutcome{Result: &cliv1.SupervisorOutcome_Flow{Flow: &cliv1.FlowEnd{Verdict: &cliv1.FlowEnd_Passed{}}}},
		&cliv1.SupervisorOutcome{Result: &cliv1.SupervisorOutcome_Flow{Flow: &cliv1.FlowEnd{}}},
	)
	f.Fuzz(func(t *testing.T, data []byte) {
		message := &cliv1.SupervisorOutcome{}
		decode(t, data, message)
		first := wire.ToOutcome(message)
		settled := lifecycle.Settle("j1", core.JobRunning, first, time.Unix(1, 0))
		if settled.Transition.To == core.JobSucceeded && !saysSucceeded(message) {
			t.Fatalf("%v settled as a success", message)
		}
		if again := wire.ToOutcome(wire.Outcome(first)); !reflect.DeepEqual(again, first) {
			t.Fatalf("a decoded outcome changed when encoded and decoded again:\n got %+v\nwant %+v", again, first)
		}
	})
}

// FuzzFailureFromStatus builds the status a peer's reply carries from arbitrary bytes, with the
// Failure detail among its details, and reads it as a caller would.
func FuzzFailureFromStatus(f *testing.F) {
	for _, failure := range []*cliv1.Failure{{}, {Kind: cliv1.FailureKind_FAILURE_KIND_NOT_FOUND, Message: "gone"}, {Kind: 99, Message: "unknown kind"}} {
		detail, err := anypb.New(failure)
		if err != nil {
			f.Fatal(err)
		}
		data, err := proto.Marshal(&status.Status{Code: 5, Message: "m", Details: []*anypb.Any{detail}})
		if err != nil {
			f.Fatal(err)
		}
		f.Add(data)
	}
	f.Add([]byte(nil))
	f.Fuzz(func(t *testing.T, data []byte) {
		message := &status.Status{}
		decode(t, data, message)
		err := statuspb.FromProto(message).Err()
		if err == nil {
			return
		}
		failure, ok := wire.FailureOf(err)
		wire.Broken(err)
		if ok && !slices.Contains(failureKinds, failure.Kind) {
			t.Fatalf("a status read as the failure kind %q, which is not one of the kinds", failure.Kind)
		}
	})
}

// saysSucceeded is whether the message says, in the members that are sent, that the job
// succeeded.
func saysSucceeded(m *cliv1.SupervisorOutcome) bool {
	switch r := m.GetResult().(type) {
	case *cliv1.SupervisorOutcome_Command:
		e := r.Command.GetEnded()
		return e != nil && e.GetKind() == cliv1.ExecutionKind_EXECUTION_KIND_EXITED && e.GetExitCode() == 0
	case *cliv1.SupervisorOutcome_Flow:
		return r.Flow.GetPassed() != nil
	}
	return false
}
