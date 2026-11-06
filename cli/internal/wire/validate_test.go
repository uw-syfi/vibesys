package wire_test

import (
	"context"
	"path/filepath"
	"strings"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/durationpb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

const goodCommit = "0123456789abcdef0123456789abcdef01234567"

func goodContext() *cliv1.CallContext {
	return &cliv1.CallContext{Repository: "/repo", Task: "task"}
}

func goodSpec() *cliv1.JobSpec {
	return &cliv1.JobSpec{Commit: goodCommit, Settings: &cliv1.JobSpec_Check{Check: &cliv1.CheckSettings{}}}
}

// Each request below is valid, and a field is spoiled to make it break exactly one rule.
func TestARequestThatBreaksARuleIsRefusedNamingTheField(t *testing.T) {
	for _, tc := range []struct {
		name  string
		bad   proto.Message
		field string
	}{
		{"relative repository", &cliv1.ShowRequest{Context: &cliv1.CallContext{Repository: "repo", Task: "task"}, Commit: goodCommit}, "context.repository"},
		{"no task", &cliv1.ShowRequest{Context: &cliv1.CallContext{Repository: "/repo"}, Commit: goodCommit}, "context.task"},
		{"task starting with a dot", &cliv1.ShowRequest{Context: &cliv1.CallContext{Repository: "/repo", Task: ".x"}, Commit: goodCommit}, "context.task"},
		{"short commit", &cliv1.ShowRequest{Context: goodContext(), Commit: "abc"}, "commit"},
		{"upper-case commit", &cliv1.PromoteRequest{Context: goodContext(), Commit: strings.ToUpper(goodCommit)}, "commit"},
		{"bad against", &cliv1.CompareRequest{Context: goodContext(), Commit: goodCommit, Against: "main"}, "against"},
		{"no spec", &cliv1.SubmitRequest{Context: goodContext()}, "spec"},
		{"no jobs to wait for", &cliv1.WaitRequest{Context: goodContext()}, "job_ids"},
		{"empty job id", &cliv1.WaitRequest{Context: goodContext(), JobIds: []string{"j1", ""}}, "job_ids[1]"},
		{"zero timeout", &cliv1.WaitRequest{Context: goodContext(), JobIds: []string{"j1"}, Timeout: durationpb.New(0)}, "timeout"},
		{"empty note", &cliv1.AddNoteRequest{Context: goodContext(), JobId: "j1"}, "text"},
		{"no job to cancel", &cliv1.CancelRequest{Context: goodContext()}, "job_id"},
		{"bad filter commit", &cliv1.ListJobsRequest{Context: goodContext(), Commit: "zz"}, "commit"},
		{"unspecified state filter", &cliv1.ListJobsRequest{Context: goodContext(), States: []cliv1.JobState{cliv1.JobState_JOB_STATE_UNSPECIFIED}}, "states[0]"},
		{"edit that sets nothing", &cliv1.EditPlanRequest{Context: goodContext(), Number: 1}, "EditPlanRequest"},
		{"plan zero", &cliv1.ViewPlanRequest{Context: goodContext()}, "number"},
		{"hello from a supervisor", &cliv1.HelloRequest{Component: cliv1.Component_COMPONENT_SUPERVISOR}, "component"},
		{"handshake from a cli", &cliv1.HandshakeRequest{Component: cliv1.Component_COMPONENT_CLI}, "component"},
		{"launch without a spec", &cliv1.Launch{Repository: "/repo", Task: "t", JobId: "j"}, "spec"},
		{"plan command without a limit", &cliv1.PlanCommand{Argv: []string{"x"}}, "timeout"},
		{"owner of generation zero", &cliv1.OwnerFile{Format: 5}, "generation"},
		{"unversioned file", &cliv1.FormatFile{}, "format"},
		{"index entry with a relative repository", &cliv1.TaskFile{Format: 5, Repository: "repo", Task: "t"}, "repository"},
		{"run record without a launch", &cliv1.RunRecord{BootId: "b"}, "launch"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			err := wire.Validate(tc.bad)
			if err == nil {
				t.Fatalf("%T was accepted", tc.bad)
			}
			if !wire.IsInvalidInput(err) {
				t.Fatalf("refusal is not invalid input: %v", err)
			}
			if !strings.Contains(err.Error(), tc.field+":") {
				t.Fatalf("refusal does not name %q: %v", tc.field, err)
			}
		})
	}
}

// What a released build wrote or sent must still pass: each case is a message of the earliest
// shape that build produced, so a rule that tightens it fails here.
func TestWhatEarlierBuildsSentStillPasses(t *testing.T) {
	for name, m := range map[string]proto.Message{
		"a run record that predates start marks and boot ids": &cliv1.RunRecord{
			Launch: &cliv1.Launch{Repository: "/repo", Task: "t", JobId: "j", Spec: goodSpec()},
		},
		"an owner record of a daemon with no place":     &cliv1.OwnerFile{Format: 2, Generation: 1},
		"a list of every job":                           &cliv1.ListJobsRequest{Context: goodContext()},
		"a compare against the head":                    &cliv1.CompareRequest{Context: goodContext(), Commit: goodCommit},
		"a wait without a timeout":                      &cliv1.WaitRequest{Context: goodContext(), JobIds: []string{"j1"}},
		"a hello from the orchestrator":                 &cliv1.HelloRequest{Component: cliv1.Component_COMPONENT_ORCHESTRATOR},
		"a cancel with no reason":                       &cliv1.CancelRequest{Context: goodContext(), JobId: "j1"},
		"an edit that changes only the body":            &cliv1.EditPlanRequest{Context: goodContext(), Number: 3, Body: proto.String("")},
		"a result file of the first saved result":       &cliv1.ResultFile{Format: 2, Seq: 0, Result: &cliv1.Result{}},
		"the submit of a check with a task of any form": &cliv1.SubmitRequest{Context: &cliv1.CallContext{Repository: "/a/b", Task: "Kv-1.2_x"}, Spec: goodSpec()},
	} {
		if err := wire.Validate(m); err != nil {
			t.Errorf("%s was refused: %v", name, err)
		}
	}
}

// The rules and the domain say the same thing: a value the rules accept is one the domain
// accepts, and the other way round, over strings built from the alphabets that matter.
func TestTheRulesAgreeWithTheDomainOnCommitsTasksAndRepositories(t *testing.T) {
	r := newRand(77)
	pick := func(alphabet string, n int) string {
		var b strings.Builder
		for range n {
			b.WriteByte(alphabet[r.IntN(len(alphabet))])
		}
		return b.String()
	}
	for range 3000 {
		commit := pick("0123456789abcdefABCDEFg", 38+r.IntN(5))
		if got, want := wire.Validate(&cliv1.ShowRequest{Context: goodContext(), Commit: commit}) == nil, core.Commit(commit).Valid(); got != want {
			t.Fatalf("commit %q: rules accept %v, domain accepts %v", commit, got, want)
		}
		task := pick("aZ09._- /", 1+r.IntN(6))
		_, domain := core.ParseTaskName("task", task)
		if got := wire.Validate(&cliv1.ShowRequest{Context: &cliv1.CallContext{Repository: "/r", Task: task}, Commit: goodCommit}) == nil; got != (domain == nil) {
			t.Fatalf("task %q: rules accept %v, domain accepts %v", task, got, domain == nil)
		}
		repo := pick("/ab.", r.IntN(5))
		if got := wire.Validate(&cliv1.ShowRequest{Context: &cliv1.CallContext{Repository: repo, Task: "t"}, Commit: goodCommit}) == nil; got != filepath.IsAbs(repo) {
			t.Fatalf("repository %q: rules accept %v, domain accepts %v", repo, got, filepath.IsAbs(repo))
		}
	}
}

// A request that breaks a rule never reaches the handler, and the caller gets INVALID_ARGUMENT
// with the field named; a valid one reaches it untouched.
func TestTheInterceptorRefusesBeforeTheHandlerAndPassesAValidRequest(t *testing.T) {
	intercept := wire.RequestValidator()
	reached := 0
	handler := func(context.Context, any) (any, error) { reached++; return "ok", nil }

	_, err := intercept(t.Context(), &cliv1.CancelRequest{Context: goodContext()}, &grpc.UnaryServerInfo{}, handler)
	if reached != 0 || status.Code(err) != codes.InvalidArgument || !strings.Contains(err.Error(), "job_id") {
		t.Fatalf("a request with no job id reached %d times, got %v", reached, err)
	}
	got, err := intercept(t.Context(), &cliv1.CancelRequest{Context: goodContext(), JobId: "j1"}, &grpc.UnaryServerInfo{}, handler)
	if err != nil || got != "ok" || reached != 1 {
		t.Fatalf("a valid request gave %v, %v after %d calls", got, err, reached)
	}
}

type fakeStream struct {
	grpc.ServerStream
	next proto.Message
}

func (s fakeStream) RecvMsg(m any) error {
	proto.Reset(m.(proto.Message))
	proto.Merge(m.(proto.Message), s.next)
	return nil
}

func TestTheStreamInterceptorValidatesWhatTheServerReceives(t *testing.T) {
	intercept := wire.StreamRequestValidator()
	run := func(request proto.Message) error {
		return intercept(nil, fakeStream{next: request}, &grpc.StreamServerInfo{}, func(_ any, s grpc.ServerStream) error {
			return s.RecvMsg(&cliv1.WatchTaskRequest{})
		})
	}
	if err := run(&cliv1.WatchTaskRequest{Context: goodContext(), JudgedCommits: 101}); status.Code(err) != codes.InvalidArgument || !strings.Contains(err.Error(), "judged_commits") {
		t.Fatalf("an over-large window gave %v", err)
	}
	if err := run(&cliv1.WatchTaskRequest{Context: &cliv1.CallContext{Repository: "x", Task: "t"}}); status.Code(err) != codes.InvalidArgument || !strings.Contains(err.Error(), "context.repository") {
		t.Fatalf("a relative repository gave %v", err)
	}
	if err := run(&cliv1.WatchTaskRequest{Context: goodContext()}); err != nil {
		t.Fatalf("a valid watch gave %v", err)
	}
}

// FuzzValidateRequest feeds arbitrary bytes, as every request type of the daemon and the
// supervisor, to the validator: it never panics, and a refusal is invalid input that names a field.
func FuzzValidateRequest(f *testing.F) {
	types := []func() proto.Message{
		func() proto.Message { return &cliv1.HelloRequest{} },
		func() proto.Message { return &cliv1.SubmitRequest{} },
		func() proto.Message { return &cliv1.WaitRequest{} },
		func() proto.Message { return &cliv1.CancelRequest{} },
		func() proto.Message { return &cliv1.AddNoteRequest{} },
		func() proto.Message { return &cliv1.ListJobsRequest{} },
		func() proto.Message { return &cliv1.ReadLogsRequest{} },
		func() proto.Message { return &cliv1.ShowRequest{} },
		func() proto.Message { return &cliv1.CompareRequest{} },
		func() proto.Message { return &cliv1.PromoteRequest{} },
		func() proto.Message { return &cliv1.AddToFrontierRequest{} },
		func() proto.Message { return &cliv1.CreatePlanRequest{} },
		func() proto.Message { return &cliv1.EditPlanRequest{} },
		func() proto.Message { return &cliv1.ClosePlanRequest{} },
		func() proto.Message { return &cliv1.WatchTaskRequest{} },
		func() proto.Message { return &cliv1.HandshakeRequest{} },
		func() proto.Message { return &cliv1.RunRecord{} },
	}
	for i, make := range types {
		valid := make()
		data, err := proto.Marshal(valid)
		if err != nil {
			f.Fatal(err)
		}
		f.Add(uint8(i), data)
	}
	f.Add(uint8(1), mustMarshal(f, &cliv1.SubmitRequest{Context: goodContext(), Spec: goodSpec()}))
	f.Add(uint8(2), mustMarshal(f, &cliv1.WaitRequest{Context: goodContext(), JobIds: []string{"j"}}))
	f.Fuzz(func(t *testing.T, which uint8, data []byte) {
		m := types[int(which)%len(types)]()
		decode(t, data, m)
		err := wire.Validate(m)
		if err == nil {
			return
		}
		if !wire.IsInvalidInput(err) {
			t.Fatalf("%T refused with %v, which is not invalid input", m, err)
		}
		if !strings.Contains(err.Error(), ":") {
			t.Fatalf("%T refusal names no field: %v", m, err)
		}
	})
}

func mustMarshal(f *testing.F, m proto.Message) []byte {
	f.Helper()
	data, err := proto.Marshal(m)
	if err != nil {
		f.Fatal(err)
	}
	return data
}
