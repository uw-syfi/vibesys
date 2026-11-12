package wire_test

import (
	"math"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	"google.golang.org/genproto/googleapis/rpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/anypb"
	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/alloc"
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

// A command job's settings, decoded from anything, are what the job runs: a known image or none, a
// wrapper of no empty word, no retired profile request, a candidate with a Dockerfile and a context,
// and a server with a program and a startup time.
func FuzzDecodeCommandSettings(f *testing.F) {
	seedMessages(f,
		&cliv1.CommandSettings{Image: cliv1.CommandImage_COMMAND_IMAGE_HARNESS, Argv: []string{"load"}, Wrap: []string{"nsys", "profile"}, Candidate: &cliv1.CandidateSetup{
			Build: &cliv1.CandidateBuild{Dockerfile: "serve/Dockerfile", Context: "serve", AllowedBases: []string{"python:3.12"}},
			Serve: &cliv1.CandidateServe{Argv: []string{"serve", "${PORT}"}, Ready: "/health", StartupTimeout: durationpb.New(time.Minute)},
		}},
		&cliv1.CommandSettings{Image: cliv1.CommandImage_COMMAND_IMAGE_HARNESS, Argv: []string{"x"}, Wrap: []string{""}},
		&cliv1.CommandSettings{Argv: []string{"x"}, Image: 7},
		&cliv1.CommandSettings{Image: cliv1.CommandImage_COMMAND_IMAGE_HARNESS, Argv: []string{"x"}, Profile: &cliv1.ProfileRequest{Wrap: []string{"nsys"}}},
		&cliv1.CommandSettings{Candidate: &cliv1.CandidateSetup{Build: &cliv1.CandidateBuild{}}},
	)
	f.Fuzz(func(t *testing.T, data []byte) {
		message := &cliv1.CommandSettings{}
		decode(t, data, message)
		if i, err := wire.ToCommandImage("image", message.GetImage()); err == nil && i != core.ImageUnspecified && !slices.Contains(core.CommandImages(), i) {
			t.Fatalf("a decoded image %q is not one", i)
		}
		if c, err := wire.ToCommandCandidate("candidate", message.GetCandidate()); err == nil && c != nil && (c.Build.Dockerfile == "" || c.Build.Context == "") {
			t.Fatalf("a decoded candidate has no Dockerfile or context: %+v", c.Build)
		}
		spec, err := wire.ToJobSpec("spec", &cliv1.JobSpec{Commit: strings.Repeat("a", 40), Settings: &cliv1.JobSpec_Command{Command: message}})
		if err != nil {
			return
		}
		c := spec.Settings.(core.CommandSettings)
		switch {
		case message.GetProfile() != nil:
			t.Fatalf("a retired profile request was accepted: %+v", message)
		case slices.Contains(c.Wrap, ""):
			t.Fatalf("a decoded wrapper has an empty word: %+v", c.Wrap)
		case c.Image != core.ImageUnspecified && !slices.Contains(core.CommandImages(), c.Image):
			t.Fatalf("a decoded image %q is not one", c.Image)
		}
		if cand := c.Candidate; cand != nil {
			if cand.Build.Dockerfile == "" || cand.Build.Context == "" {
				t.Fatalf("a decoded candidate has no Dockerfile or context: %+v", cand.Build)
			}
			if s := cand.Serve; s != nil && (len(s.Argv) == 0 || s.StartupTimeout <= 0) {
				t.Fatalf("a decoded server has no program or no startup time: %+v", s)
			}
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
		&cliv1.Plan{Number: 3, Status: cliv1.PlanStatus_PLAN_STATUS_PARKED, GainBound: &cliv1.PlanGainBound{Share: 0.05, RelativeTo: cliv1.PlanGainReference_PLAN_GAIN_REFERENCE_HEADLINE}},
		&cliv1.Plan{
			Number: 4, Status: cliv1.PlanStatus_PLAN_STATUS_WON, Workstream: 2,
			Origin: &cliv1.RecordOrigin{RunId: "run-1", Agent: "lead", At: timestamppb.New(time.Date(2026, 11, 7, 0, 0, 0, 0, time.UTC))},
			Transitions: []*cliv1.PlanTransition{
				{Status: cliv1.PlanStatus_PLAN_STATUS_OPEN, Origin: &cliv1.RecordOrigin{At: timestamppb.New(time.Date(2026, 11, 7, 0, 0, 0, 0, time.UTC))}},
				{Status: 99, Origin: &cliv1.RecordOrigin{Agent: "a"}},
			},
		},
	)
	f.Fuzz(func(t *testing.T, data []byte) {
		message := &cliv1.Plan{}
		decode(t, data, message)
		// A plan is refused exactly when its status, the status of one of its transitions, or its
		// gain bound is one that is refused alone.
		_, statusErr := wire.ToPlanStatus("status", message.GetStatus())
		for _, tr := range message.GetTransitions() {
			if _, err := wire.ToPlanStatus("transition", tr.GetStatus()); err != nil {
				statusErr = err
			}
		}
		if _, err := wire.ToGainBound("gain_bound", message.GetGainBound()); err != nil {
			statusErr = err
		}
		first, err := wire.ToPlan(message)
		if (err != nil) != (statusErr != nil) {
			t.Fatalf("a plan with status %s: ToPlan error %v, error of its parts %v; they must agree", message.GetStatus(), err, statusErr)
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

func FuzzDecodeBound(f *testing.F) {
	at := timestamppb.New(time.Date(2026, 11, 7, 0, 0, 0, 0, time.UTC))
	valid := &cliv1.ObjectiveBound{
		Seq: 2, Origin: &cliv1.RecordOrigin{RunId: "run-1", Agent: "lead", At: at}, Objective: "tok_s",
		Direction: cliv1.Direction_DIRECTION_MAXIMIZE, Value: 15600, Unit: "tok/s", Note: "bandwidth over bytes per token",
	}
	seedMessages(f, &cliv1.ObjectiveBound{}, valid,
		&cliv1.ObjectiveBound{Seq: 1, Objective: "ttft", Direction: cliv1.Direction_DIRECTION_MINIMIZE, Value: 74},
		&cliv1.ObjectiveBound{Seq: 1, Objective: "x", Direction: 99, Value: 1},
	)
	f.Fuzz(func(t *testing.T, data []byte) {
		message := &cliv1.ObjectiveBound{}
		decode(t, data, message)
		first, err := wire.ToBound("bound", message)
		if err != nil {
			return
		}
		if first.Direction != core.Maximize && first.Direction != core.Minimize {
			t.Fatalf("a decoded bound has the direction %q, which is not one", first.Direction)
		}
		again, err := wire.ToBound("bound", wire.Bound(first))
		if err != nil {
			t.Fatalf("a decoded bound %+v is refused once encoded again: %v", first, err)
		}
		if !reflect.DeepEqual(again, first) {
			t.Fatalf("a decoded bound changed when encoded and decoded again:\n got %+v\nwant %+v", again, first)
		}
	})
}

func FuzzDecodeWorkstream(f *testing.F) {
	at := timestamppb.New(time.Date(2026, 11, 7, 0, 0, 0, 0, time.UTC))
	valid := &cliv1.Workstream{
		Number: 2, Title: "KV cache layout", Status: cliv1.WorkstreamStatus_WORKSTREAM_STATUS_PAUSED, Note: "paused: accuracy gate",
		Origin: &cliv1.RecordOrigin{RunId: "run-1", Agent: "lead", At: at}, UpdatedAt: at,
	}
	seedMessages(f, &cliv1.Workstream{}, valid,
		&cliv1.Workstream{Number: 1, Title: "x", Status: 99},
		&cliv1.Workstream{Number: 1, Title: "  padded  ", Status: cliv1.WorkstreamStatus_WORKSTREAM_STATUS_ACTIVE},
	)
	f.Fuzz(func(t *testing.T, data []byte) {
		message := &cliv1.Workstream{}
		decode(t, data, message)
		_, statusErr := wire.ToWorkstreamStatus("status", message.GetStatus())
		first, err := wire.ToWorkstream("workstream", message)
		if (err != nil) != (statusErr != nil) {
			t.Fatalf("a workstream with status %s: ToWorkstream error %v, ToWorkstreamStatus error %v; they must agree", message.GetStatus(), err, statusErr)
		}
		if err != nil {
			if !strings.Contains(err.Error(), "workstream.status") {
				t.Fatalf("the refusal %q does not name the field", err)
			}
			return
		}
		if !first.Status.Valid() {
			t.Fatalf("a decoded workstream has the status %q, which is not a status", first.Status)
		}
		again, err := wire.ToWorkstream("workstream", wire.Workstream(first))
		if err != nil {
			t.Fatalf("a decoded workstream %+v is refused once encoded again: %v", first, err)
		}
		if !reflect.DeepEqual(again, first) {
			t.Fatalf("a decoded workstream changed when encoded and decoded again:\n got %+v\nwant %+v", again, first)
		}
	})
}

func FuzzDecodeWorkstreamFilter(f *testing.F) {
	seedMessages(f,
		&cliv1.ListWorkstreamsRequest{},
		&cliv1.ListWorkstreamsRequest{Statuses: []cliv1.WorkstreamStatus{cliv1.WorkstreamStatus_WORKSTREAM_STATUS_ACTIVE, cliv1.WorkstreamStatus_WORKSTREAM_STATUS_CLOSED}},
		&cliv1.ListWorkstreamsRequest{Statuses: []cliv1.WorkstreamStatus{99, -1, 0}},
	)
	f.Fuzz(func(t *testing.T, data []byte) {
		message := &cliv1.ListWorkstreamsRequest{}
		decode(t, data, message)
		filter, err := wire.ToWorkstreamFilter(message)
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

func FuzzDecodeGainBound(f *testing.F) {
	headline := cliv1.PlanGainReference_PLAN_GAIN_REFERENCE_HEADLINE
	seedMessages(f,
		&cliv1.PlanGainBound{},
		&cliv1.PlanGainBound{Share: 0.1, RelativeTo: headline},
		&cliv1.PlanGainBound{Share: 7, RelativeTo: headline},
		&cliv1.PlanGainBound{Share: -1, RelativeTo: headline},
		&cliv1.PlanGainBound{Share: math.NaN(), RelativeTo: headline},
		&cliv1.PlanGainBound{Share: math.Inf(1), RelativeTo: headline},
		&cliv1.PlanGainBound{Share: 0.1, RelativeTo: 99},
	)
	f.Fuzz(func(t *testing.T, data []byte) {
		message := &cliv1.PlanGainBound{}
		decode(t, data, message)
		bound, err := wire.ToGainBound("gain_bound", message)
		if err != nil {
			if !strings.Contains(err.Error(), "gain_bound") {
				t.Fatalf("the refusal %q does not name the field", err)
			}
			return
		}
		if bound == nil || !(bound.Share >= 0) || math.IsInf(bound.Share, 0) || !bound.RelativeTo.Valid() {
			t.Fatalf("a decoded bound %+v is not a finite share of a reference", bound)
		}
		if again, err := wire.ToGainBound("gain_bound", wire.GainBound(bound)); err != nil || *again != *bound {
			t.Fatalf("a decoded bound %+v came back as %+v, %v", bound, again, err)
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

func FuzzDecodeLeaseRecord(f *testing.F) {
	r := newRand(20)
	f.Add([]byte(nil))
	for range 8 {
		data, err := wire.EncodeLeaseRecord(anyHolder(r))
		if err != nil {
			f.Fatal(err)
		}
		f.Add(data)
	}
	f.Add([]byte(`{"user":"a","pid":3,"future":true}`))
	f.Fuzz(func(t *testing.T, data []byte) {
		first, err := wire.DecodeLeaseRecord(data)
		if err != nil {
			if !wire.IsInvalidInput(err) || !reflect.DeepEqual(first, core.LeaseHolder{}) {
				t.Fatalf("%q refused as %v with the holder %+v", data, err, first)
			}
			return
		}
		out, err := wire.EncodeLeaseRecord(first)
		if err != nil {
			t.Fatalf("a decoded holder %+v cannot be written: %v", first, err)
		}
		again, err := wire.DecodeLeaseRecord(out)
		if err != nil || !reflect.DeepEqual(again, first) {
			t.Fatalf("a decoded holder changed on the way back: %+v, %v; want %+v", again, err, first)
		}
	})
}

// FuzzDecodeLeaseRecordMessage hands the message decoder what a peer could send.
func FuzzDecodeLeaseRecordMessage(f *testing.F) {
	seedMessages(f, wire.LeaseRecord(core.LeaseHolder{User: "u", PID: 1}), &cliv1.LeaseRecord{Pid: -1})
	f.Fuzz(func(t *testing.T, data []byte) {
		m := &cliv1.LeaseRecord{}
		if proto.Unmarshal(data, m) != nil {
			t.Skip()
		}
		if _, err := wire.ToLeaseHolder(m); err != nil && !wire.IsInvalidInput(err) {
			t.Fatalf("refused with %v, which is not invalid input", err)
		}
	})
}

func FuzzDecodeNodeReading(f *testing.F) {
	at := time.Unix(1e9, 0).UTC()
	seedMessages(f, wire.NodeReading(at, map[string]string{"uname": "Linux"}), &cliv1.NodeReading{})
	f.Fuzz(func(t *testing.T, data []byte) {
		m := &cliv1.NodeReading{}
		if proto.Unmarshal(data, m) != nil {
			t.Skip()
		}
		gotAt, out, err := wire.ToNodeReading(m)
		if err != nil {
			if !wire.IsInvalidInput(err) {
				t.Fatalf("refused with %v, which is not invalid input", err)
			}
			return
		}
		enc, err := wire.EncodeNodeReading(gotAt, out)
		if err != nil {
			t.Fatal(err)
		}
		at2, out2, err := wire.DecodeNodeReading(enc)
		if err != nil || !at2.Equal(gotAt) || !reflect.DeepEqual(out2, out) {
			t.Fatalf("a decoded reading changed on the way back: %v %v, %v", at2, out2, err)
		}
	})
}

func FuzzDecodeNodeReadingJSON(f *testing.F) {
	f.Add([]byte(`{"at":"2026-01-01T00:00:00Z","outputs":{"a":"b"}}`))
	f.Fuzz(func(t *testing.T, data []byte) {
		if _, out, err := wire.DecodeNodeReading(data); err != nil && (out != nil || !wire.IsInvalidInput(err)) {
			t.Fatalf("%q refused as %v with %v", data, err, out)
		}
	})
}

func FuzzDecodeAllocationInput(f *testing.F) {
	var seeds []proto.Message
	for _, in := range everyInput() {
		m, err := wire.FromAllocationInput(in)
		if err != nil {
			f.Fatal(err)
		}
		seeds = append(seeds, m)
	}
	seedMessages(f, seeds...)
	f.Fuzz(func(t *testing.T, data []byte) {
		message := &cliv1.AllocationInput{}
		decode(t, data, message)
		first, err := wire.ToAllocationInput(message)
		if err != nil {
			if !wire.IsInvalidInput(err) {
				t.Fatalf("a refused input gave %v, want an invalid-input failure", err)
			}
			return
		}
		encoded, err := wire.FromAllocationInput(first)
		if err != nil {
			t.Fatalf("a decoded input %+v does not encode: %v", first, err)
		}
		again, err := wire.ToAllocationInput(encoded)
		if err != nil {
			t.Fatalf("a decoded input changed when encoded and decoded again: %v", err)
		}
		reencoded, err := wire.FromAllocationInput(again)
		if err != nil || !proto.Equal(reencoded, encoded) {
			t.Fatalf("a decoded input changed when encoded and decoded again: %v, %v; want %v", reencoded, err, encoded)
		}
	})
}

func FuzzDecodeRenewalOrder(f *testing.F) {
	seedMessages(f,
		wire.FromRenewalOrder(alloc.RenewalOrder{Lead: time.Hour, For: 12 * time.Hour, Until: alloc.UntilRunEnd{}}),
		wire.FromRenewalOrder(alloc.RenewalOrder{Lead: time.Minute, For: time.Hour, Until: alloc.UntilTime{At: logAt}}))
	f.Fuzz(func(t *testing.T, data []byte) {
		message := &cliv1.RenewalOrder{}
		decode(t, data, message)
		first, err := wire.ToRenewalOrder(message)
		if err != nil {
			if !wire.IsInvalidInput(err) {
				t.Fatalf("a refused order gave %v, want an invalid-input failure", err)
			}
			return
		}
		again, err := wire.ToRenewalOrder(wire.FromRenewalOrder(first))
		if err != nil || again.Lead != first.Lead || again.For != first.For {
			t.Fatalf("a decoded order changed when encoded and decoded again: %+v, %v; want %+v", again, err, first)
		}
	})
}
