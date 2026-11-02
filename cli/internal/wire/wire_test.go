package wire_test

import (
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"reflect"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/durationpb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

func newRand(seed uint64) *rand.Rand { return rand.New(rand.NewPCG(seed, seed^0x9e3779b97f4a7c15)) }

var epoch = time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)

func pick[T any](r *rand.Rand, xs ...T) T { return xs[r.IntN(len(xs))] }

func words(r *rand.Rand, n int) []string {
	if n == 0 {
		return nil
	}
	out := make([]string, n)
	for i := range out {
		out[i] = pick(r, "make", "-j4", "./run.sh", "--flag=1", "ü", "a b")
	}
	return out
}

func anyJob(r *rand.Rand) core.Job {
	j := core.Job{
		ID:          core.JobID(fmt.Sprintf("j%d", r.IntN(100))),
		State:       pick(r, core.JobStates()...),
		SubmittedAt: epoch.Add(time.Duration(r.IntN(1e6)) * time.Millisecond),
	}
	commit := core.Commit(fmt.Sprintf("%040x", r.Uint64()))
	switch r.IntN(3) {
	case 0:
		j.Spec = core.JobSpec{Commit: commit, Settings: core.CheckSettings{}}
		j.Key = "k1"
	case 1:
		cs := core.CommandSettings{Argv: append([]string{"run"}, words(r, r.IntN(3))...), Timeout: time.Duration(r.IntN(100)) * time.Second}
		if r.IntN(2) == 0 {
			cs.Service = &core.ServiceSpec{Argv: words(r, 1+r.IntN(2)), Probe: words(r, r.IntN(2))}
		}
		j.Spec = core.JobSpec{Commit: commit, Environment: pick(r, core.EnvironmentAuto, core.EnvironmentHost, core.EnvironmentDocker), Settings: cs}
	default:
		j.Spec = core.JobSpec{Commit: commit, Settings: core.MeasureSettings{Samples: uint32(r.IntN(9)), Recheck: r.IntN(2) == 0}}
		j.Key = "k2"
	}
	if r.IntN(2) == 0 {
		fp := core.Fingerprint{Kind: core.EnvironmentHost, Backend: pick(r, core.BackendCPU, core.BackendCUDA)}
		if r.IntN(2) == 0 {
			fp.Facts = map[string]string{"os": "linux"}
		}
		j.Environment = &fp
	}
	for range r.IntN(3) {
		j.Notes = append(j.Notes, core.Note{Text: pick(r, "a", "ü", ""), At: epoch.Add(time.Duration(r.IntN(1e6)) * time.Nanosecond), Agent: pick(r, "", "claude")})
	}
	for range r.IntN(3) {
		j.Plans = append(j.Plans, core.PlanNumber(1+r.IntN(5)))
	}
	if r.IntN(2) == 0 {
		at := epoch.Add(time.Hour)
		j.StartedAt = &at
	}
	if r.IntN(2) == 0 {
		at := epoch.Add(2 * time.Hour)
		j.FinishedAt = &at
		code := r.IntN(5) - 1
		j.ExitCode = &code
	}
	j.CancelReason = pick(r, "", "stop it")
	return j
}

func cloneSpec(s *cliv1.JobSpec) *cliv1.JobSpec { return proto.Clone(s).(*cliv1.JobSpec) }

func TestAJobSurvivesTheContractAndBack(t *testing.T) {
	r := newRand(1)
	for range 1000 {
		j := anyJob(r)
		got, err := wire.ToJob(wire.Job(j))
		if err != nil {
			t.Fatalf("job %+v: %v", j, err)
		}
		if !reflect.DeepEqual(got, j) {
			t.Fatalf("job came back changed:\n got %+v\nwant %+v", got, j)
		}
	}
}

// Every value the contract's enums can take is either one the domain names or is refused with the
// field named, so adding a value to the contract without a table entry fails here.
func TestEveryContractEnumValueIsTranslatedOrRefused(t *testing.T) {
	for n, name := range cliv1.JobState_name {
		state, err := wire.ToJobState("state", cliv1.JobState(n))
		if name == "JOB_STATE_UNSPECIFIED" {
			if err == nil {
				t.Fatalf("%s was accepted as %q", name, state)
			}
			continue
		}
		if err != nil || wire.JobState(state) != cliv1.JobState(n) {
			t.Fatalf("%s: got %q, %v", name, state, err)
		}
	}
	for _, s := range core.JobStates() {
		if wire.JobState(s) == cliv1.JobState_JOB_STATE_UNSPECIFIED {
			t.Fatalf("state %q has no contract value", s)
		}
	}
	for n, name := range cliv1.EnvironmentKind_name {
		kind, err := wire.ToEnvironmentKind("environment", cliv1.EnvironmentKind(n))
		if err != nil || wire.EnvironmentKind(kind) != cliv1.EnvironmentKind(n) {
			t.Fatalf("%s: got %q, %v", name, kind, err)
		}
	}
	for n, name := range cliv1.ExecutionKind_name {
		kind := wire.ToExecKind(cliv1.ExecutionKind(n))
		if name == "EXECUTION_KIND_UNSPECIFIED" && kind != core.ExecUnknown {
			t.Fatalf("an unspecified ending is %q, want unknown", kind)
		}
		if name != "EXECUTION_KIND_UNSPECIFIED" && wire.ExecutionKind(kind) != cliv1.ExecutionKind(n) {
			t.Fatalf("%s: got %q", name, kind)
		}
	}
	if got := wire.ToExecKind(cliv1.ExecutionKind(99)); got != core.ExecUnknown {
		t.Fatalf("an ending from a newer build is %q, want unknown and never a success", got)
	}
	for n, name := range cliv1.FailureKind_name {
		if name == "FAILURE_KIND_UNSPECIFIED" {
			continue
		}
		f := wire.ToFailure(&cliv1.Failure{Kind: cliv1.FailureKind(n), Message: "m"})
		if f.Kind == core.FailureInternal && name != "FAILURE_KIND_INTERNAL" {
			t.Fatalf("%s is read as internal", name)
		}
	}
}

func TestASubmittedSpecIsHeldToWhatTheStoreCanHold(t *testing.T) {
	good := &cliv1.JobSpec{
		Commit:   strings.Repeat("a", 40),
		Settings: &cliv1.JobSpec_Command{Command: &cliv1.CommandSettings{Argv: []string{"true"}}},
	}
	if _, err := wire.ToJobSpec("spec", good); err != nil {
		t.Fatalf("a good spec was refused: %v", err)
	}
	cases := map[string]func(*cliv1.JobSpec){
		"spec.commit":               func(s *cliv1.JobSpec) { s.Commit = "HEAD" },
		"spec.commit ":              func(s *cliv1.JobSpec) { s.Commit = strings.Repeat("A", 40) },
		"spec.environment":          func(s *cliv1.JobSpec) { s.Environment = 77 },
		"spec: no job settings":     func(s *cliv1.JobSpec) { s.Settings = nil },
		"spec.command.argv":         func(s *cliv1.JobSpec) { s.GetCommand().Argv = nil },
		"spec.command.argv ":        func(s *cliv1.JobSpec) { s.GetCommand().Argv = []string{""} },
		"spec.command.service.argv": func(s *cliv1.JobSpec) { s.GetCommand().Service = &cliv1.ServiceSpec{} },
		"spec.command.timeout":      func(s *cliv1.JobSpec) { s.GetCommand().Timeout = durationpb.New(-time.Second) },
		"spec.command.service.probe": func(s *cliv1.JobSpec) {
			s.GetCommand().Service = &cliv1.ServiceSpec{Argv: []string{"x"}, Probe: []string{""}}
		},
	}
	for want, mutate := range cases {
		spec := cloneSpec(good)
		mutate(spec)
		_, err := wire.ToJobSpec("spec", spec)
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, strings.TrimSpace(want)) {
			t.Fatalf("%s: got %v, want invalid input naming the field", want, err)
		}
	}
	if _, err := wire.ToJobSpec("spec", nil); err == nil {
		t.Fatal("a missing spec was accepted")
	}
}

func TestAFailureKeepsItsKindAcrossAGRPCStatus(t *testing.T) {
	kinds := []core.FailureKind{core.FailureInvalidInput, core.FailureNotFound, core.FailureConflict, core.FailureUnavailable,
		core.FailureIncompatible, core.FailureEnvironmentMismatch, core.FailureNotMeasured, core.FailureInternal}
	for _, k := range kinds {
		in := core.Failure{Kind: k, Message: "something " + string(k)}
		got, ok := wire.FailureOf(wire.Error(fmt.Errorf("while testing: %w", &in)))
		if !ok || got != in {
			t.Fatalf("%v came back as %v, %v", in, got, ok)
		}
	}
	if _, ok := wire.FailureOf(wire.Error(context.Canceled)); ok {
		t.Fatal("a cancelled call is the caller's own cancel, not a failure")
	}
	if got, _ := wire.FailureOf(wire.Error(errors.New("disk on fire"))); got.Kind != core.FailureInternal || !strings.Contains(got.Message, "disk on fire") {
		t.Fatalf("an unknown error came back as %v", got)
	}
	if wire.Error(nil) != nil {
		t.Fatal("no error became an error")
	}
}

func TestAnOutcomeSurvivesTheContractAndBack(t *testing.T) {
	r := newRand(3)
	kinds := []core.ExecKind{core.ExecExited, core.ExecTimedOut, core.ExecCancelled, core.ExecServiceNotReady, core.ExecServiceExited, core.ExecUnknown}
	for range 500 {
		o := lifecycle.Outcome{}
		if r.IntN(5) == 0 {
			o.NotAttempted = &core.Failure{Kind: pick(r, core.FailureUnavailable, core.FailureInternal), Message: "no image"}
		} else {
			o.Exec = core.ExecOutcome{Kind: pick(r, kinds...)}
			if o.Exec.Kind == core.ExecExited {
				o.Exec.ExitCode = r.IntN(300) - 1
			}
		}
		if r.IntN(2) == 0 {
			o.CancelAsked, o.CancelReason = true, pick(r, "", "stop")
		}
		if got := wire.ToOutcome(wire.Outcome(o)); !reflect.DeepEqual(got, o) {
			t.Fatalf("outcome came back changed: got %+v, want %+v", got, o)
		}
	}
	if got := wire.ToOutcome(&cliv1.SupervisorOutcome{}); got.Exec.Kind != core.ExecUnknown {
		t.Fatalf("an empty outcome is %+v, want unknown", got)
	}
}

func TestALaunchCarriesWhatASupervisorIsPromised(t *testing.T) {
	good := lifecycle.Launch{Repository: "/repo", Task: "kv", Job: "j1", Spec: core.JobSpec{
		Commit: core.Commit(strings.Repeat("b", 40)), Environment: core.EnvironmentHost,
		Settings: core.CommandSettings{Argv: []string{"true"}, Timeout: time.Minute},
	}}
	got, err := wire.ToLaunch(wire.Launch(good))
	if err != nil || !reflect.DeepEqual(got, good) {
		t.Fatalf("a launch came back as %+v, %v", got, err)
	}
	for name, mutate := range map[string]func(*lifecycle.Launch){
		"environment": func(l *lifecycle.Launch) { l.Spec.Environment = core.EnvironmentAuto },
		"timeout": func(l *lifecycle.Launch) {
			c := l.Spec.Settings.(core.CommandSettings)
			c.Timeout = 0
			l.Spec.Settings = c
		},
		"repository": func(l *lifecycle.Launch) { l.Repository = "" },
	} {
		l := good
		mutate(&l)
		if _, err := wire.ToLaunch(wire.Launch(l)); err == nil || !strings.Contains(err.Error(), name) {
			t.Fatalf("a launch with no %s: %v", name, err)
		}
	}
}

func TestTheWaitRuleOverTheContractIsTheDomainRule(t *testing.T) {
	r := newRand(4)
	for range 1000 {
		n := 1 + r.IntN(4)
		var jobs []*cliv1.Job
		var states []core.JobState
		for range n {
			s := pick(r, core.JobStates()...)
			states = append(states, s)
			jobs = append(jobs, &cliv1.Job{State: wire.JobState(s)})
		}
		for _, any := range []bool{false, true} {
			done, ok := wire.Await(jobs, any)
			wantDone, wantOK := lifecycle.Await(states, any)
			if done != wantDone || ok != wantOK {
				t.Fatalf("wait(any=%v) over %v: got %v, %v want %v, %v", any, states, done, ok, wantDone, wantOK)
			}
		}
	}
	if done, _ := wire.Await([]*cliv1.Job{{State: 99}}, true); done {
		t.Fatal("a wait ended on a state it cannot read")
	}
}
