package wire_test

import (
	"reflect"
	"slices"
	"strings"
	"testing"

	"google.golang.org/genproto/googleapis/rpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/anypb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
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
		// The wait rule reads whatever jobs a peer sends.
		wire.Await([]*cliv1.Job{message}, false)
		wire.Await([]*cliv1.Job{message, message}, true)
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

// A supervisor's outcome and a peer's failure decode with no error to return, so what they must
// hold is that they never panic and that an outcome that says nothing is never a success.
func FuzzDecodeOutcome(f *testing.F) {
	seedMessages(f,
		&cliv1.SupervisorOutcome{},
		&cliv1.SupervisorOutcome{Result: &cliv1.SupervisorOutcome_Executed{Executed: &cliv1.ExecutionOutcome{Kind: cliv1.ExecutionKind_EXECUTION_KIND_EXITED}}},
		&cliv1.SupervisorOutcome{Result: &cliv1.SupervisorOutcome_NotAttempted{NotAttempted: &cliv1.Failure{Message: "no image"}}},
		&cliv1.SupervisorOutcome{Result: &cliv1.SupervisorOutcome_Executed{}},
		&cliv1.SupervisorOutcome{Result: &cliv1.SupervisorOutcome_NotAttempted{}},
	)
	f.Fuzz(func(t *testing.T, data []byte) {
		message := &cliv1.SupervisorOutcome{}
		decode(t, data, message)
		first := wire.ToOutcome(message)
		if message.GetResult() == nil && first.Exec.Kind != core.ExecUnknown {
			t.Fatalf("an outcome with no result decoded as %+v, want unknown", first)
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
