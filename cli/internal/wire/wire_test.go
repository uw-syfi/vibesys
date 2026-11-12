package wire_test

import (
	"google.golang.org/genproto/googleapis/rpc/status"
	statuspb "google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/anypb"

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
		FailedStage: pick(r, core.JobStage(""), core.StageBuild, core.StageCheck, core.StageMeasure),
	}
	commit := core.Commit(fmt.Sprintf("%040x", r.Uint64()))
	switch r.IntN(3) {
	case 0:
		j.Spec = core.JobSpec{Commit: commit, Settings: core.CheckSettings{}}
		j.Key = "k1"
	case 1:
		cs := core.CommandSettings{Image: core.ImageHarness, Argv: append([]string{"run"}, words(r, r.IntN(3))...), Timeout: time.Duration(r.IntN(100)) * time.Second}
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
	for i := range r.IntN(core.MaxAttempts) {
		cause := pick(r, core.CutSupervisorDied, core.CutRebooted)
		past := core.PastAttempt{StartedAt: epoch.Add(time.Duration(i) * time.Minute), EndedAt: epoch.Add(time.Duration(i)*time.Minute + time.Second), Cut: core.CutBeforeStart{By: cause}}
		if r.IntN(2) == 0 {
			past.Cut = core.CutMidCommand{By: cause}
		}
		j.Attempts = append(j.Attempts, past)
	}
	for i := range r.IntN(3) {
		j.Submitters = append(j.Submitters, core.Submitter{Run: core.RunID(pick(r, "", "run-a", "run-b")), Agent: fmt.Sprintf("agent-%d", i), At: epoch.Add(time.Duration(i) * time.Second)})
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
	j.NoCache = r.IntN(2) == 0
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
	for n, name := range cliv1.JobStage_name {
		stage, err := wire.ToJobStage("failed_stage", cliv1.JobStage(n))
		if err != nil || wire.JobStage(stage) != cliv1.JobStage(n) {
			t.Fatalf("%s: got %q, %v", name, stage, err)
		}
	}
	if stage, err := wire.ToJobStage("failed_stage", cliv1.JobStage(99)); err == nil {
		t.Fatalf("a stage from a newer build is %q, want it refused", stage)
	}
	for n, name := range cliv1.CutCause_name {
		cause, err := wire.ToCutCause("cause", cliv1.CutCause(n))
		if name == "CUT_CAUSE_UNSPECIFIED" {
			if err == nil {
				t.Fatalf("%s was accepted as %q", name, cause)
			}
			continue
		}
		if err != nil || wire.CutCause(cause) != cliv1.CutCause(n) {
			t.Fatalf("%s: got %q, %v", name, cause, err)
		}
	}
	for n, name := range cliv1.EnvironmentKind_name {
		kind, err := wire.ToEnvironmentKind("environment", cliv1.EnvironmentKind(n))
		if err != nil || wire.EnvironmentKind(kind) != cliv1.EnvironmentKind(n) {
			t.Fatalf("%s: got %q, %v", name, kind, err)
		}
	}
	for n, name := range cliv1.ExecutionKind_name {
		kind, err := wire.ToExecKind("kind", cliv1.ExecutionKind(n))
		noEnding := name == "EXECUTION_KIND_UNSPECIFIED" || name == "EXECUTION_KIND_UNKNOWN"
		if noEnding && err == nil {
			t.Fatalf("%s is read as the ending %q, want it refused", name, kind)
		}
		if !noEnding && (err != nil || wire.ExecutionKind(kind) != cliv1.ExecutionKind(n)) {
			t.Fatalf("%s: got %q, %v", name, kind, err)
		}
	}
	if got, err := wire.ToExecKind("kind", cliv1.ExecutionKind(99)); err == nil {
		t.Fatalf("an ending from a newer build is %q, want it refused and never a success", got)
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

func TestAJobWithAsManyCutAttemptsAsItGetsIsRefused(t *testing.T) {
	msg := wire.Job(anyJob(rand.New(rand.NewPCG(1, 2))))
	msg.Attempts = nil
	for range core.MaxAttempts {
		msg.Attempts = append(msg.Attempts, &cliv1.Attempt{End: &cliv1.Attempt_CutMidCommand{CutMidCommand: cliv1.CutCause_CUT_CAUSE_REBOOTED}})
	}
	if _, err := wire.ToJob(msg); err == nil {
		t.Fatalf("a job with %d attempts cut off was accepted", core.MaxAttempts)
	}
	msg.Attempts = []*cliv1.Attempt{{}}
	if _, err := wire.ToJob(msg); err == nil {
		t.Fatal("an attempt with no end was accepted")
	}
}

func TestASubmittedSpecIsHeldToWhatTheStoreCanHold(t *testing.T) {
	good := &cliv1.JobSpec{
		Commit:   strings.Repeat("a", 40),
		Settings: &cliv1.JobSpec_Command{Command: &cliv1.CommandSettings{Image: cliv1.CommandImage_COMMAND_IMAGE_HARNESS, Argv: []string{"true"}}},
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
	kinds := []core.ExecKind{core.ExecExited, core.ExecTimedOut, core.ExecCancelled, core.ExecServiceNotReady, core.ExecServiceExited}
	exec := func() core.ExecOutcome {
		e := core.ExecOutcome{Kind: pick(r, kinds...)}
		if e.Kind == core.ExecExited {
			e.ExitCode = r.IntN(300) - 1
		}
		return e
	}
	failure := func() core.Failure {
		return core.Failure{Kind: pick(r, core.FailureUnavailable, core.FailureInternal), Message: pick(r, "", "no image")}
	}
	for range 500 {
		o := lifecycle.Outcome{}
		switch r.IntN(9) {
		case 0:
			o.End = lifecycle.CommandEnded{Attempt: core.NotStarted{Failure: failure()}}
		case 1:
			o.End = lifecycle.CommandEnded{Attempt: core.Ended{Exec: exec()}}
		case 2:
			o.End = lifecycle.CommandEnded{Attempt: core.Unobserved{Failure: failure()}}
		case 3:
			o.End = lifecycle.FlowEnded{Verdict: lifecycle.Passed{Result: aResult()}}
		case 4:
			o.End = lifecycle.FlowEnded{Verdict: lifecycle.Stopped{Exec: exec(), Stage: pick(r, core.JobStage(""), core.StageBuild, core.StageCheck, core.StageMeasure)}}
		case 5:
			o.End = lifecycle.FlowEnded{Verdict: lifecycle.NotAttempted{Failure: failure()}}
		case 6:
			o.End = lifecycle.FlowEnded{Verdict: lifecycle.Rejected{Reason: pick(r, "", "no result"), Stage: pick(r, core.JobStage(""), core.StageMeasure)}}
		case 7:
			o.End = lifecycle.FlowEnded{Verdict: lifecycle.Lost{Failure: failure()}}
		default:
		}
		if r.IntN(2) == 0 {
			o.CancelAsked, o.CancelReason = true, pick(r, "", "stop")
		}
		for range r.IntN(3) {
			o.ProcessesLeft = append(o.ProcessesLeft, lifecycle.ProcessesLeft{Allocation: pick(r, "4242", "4243"), Detail: pick(r, "", "reap: timed out")})
		}
		if err := wire.Validate(wire.Outcome(o)); err != nil {
			t.Fatalf("outcome %+v breaks the contract: %v", o, err)
		}
		if got := wire.ToOutcome(wire.Outcome(o)); !reflect.DeepEqual(got, o) {
			t.Fatalf("outcome came back changed: got %+v, want %+v", got, o)
		}
	}
	if got := wire.ToOutcome(&cliv1.SupervisorOutcome{}); got.End != nil {
		t.Fatalf("an empty outcome is %+v, want no end", got)
	}
	unreadable := &cliv1.SupervisorOutcome{Result: &cliv1.SupervisorOutcome_Flow{Flow: &cliv1.FlowEnd{Verdict: &cliv1.FlowEnd_Passed{Passed: &cliv1.Result{}}}}}
	if got := wire.ToOutcome(unreadable); !isLost(got) {
		t.Fatalf("a result that cannot be read is %+v, want lost and never a success", got)
	}
	unknownKind := &cliv1.SupervisorOutcome{Result: &cliv1.SupervisorOutcome_Command{Command: &cliv1.CommandEnd{
		Attempt: &cliv1.CommandEnd_Ended{Ended: &cliv1.ExecutionOutcome{Kind: cliv1.ExecutionKind_EXECUTION_KIND_UNKNOWN}},
	}}}
	if got := wire.ToOutcome(unknownKind); !isLost(got) {
		t.Fatalf("an ending of no kind is %+v, want one nobody saw", got)
	}
}

// isLost says an outcome is an ending nobody saw.
func isLost(o lifecycle.Outcome) bool {
	switch end := o.End.(type) {
	case lifecycle.CommandEnded:
		_, ok := end.Attempt.(core.Unobserved)
		return ok
	case lifecycle.FlowEnded:
		_, ok := end.Verdict.(lifecycle.Lost)
		return ok
	}
	return false
}

// aResult is a result with every field set, as a supervisor reports it.
func aResult() core.Result {
	at := time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)
	return core.Result{
		Job: "j1", Key: "k", Task: "t", TaskVersion: "v", Commit: "c", SchemaVersion: 1,
		Environment: core.Fingerprint{Kind: core.EnvironmentHost, Backend: core.BackendCPU, Facts: map[string]string{"os": "linux"}},
		Machine:     core.MachineFacts{HostID: "h", OS: "linux", Arch: "amd64", CPUModel: "m", LogicalCPUs: 8, MemoryBytes: 1 << 30},
		Load:        core.MachineLoad{SampledAt: at, LoadAverage1m: 0.5, CPUBusyFraction: 0.25, MemoryAvailableBytes: 1 << 20},
		RecordedAt:  at,
		Outcome: core.MeasureOutcome{
			Metric: core.Metric{Name: "rps", Direction: core.Maximize}, Samples: []float64{1, 2, 3}, CheckJob: "j0",
			Observations: []core.Observation{{Name: "p99", Values: []float64{4}}},
		},
	}
}

func TestALaunchCarriesWhatASupervisorIsPromised(t *testing.T) {
	good := lifecycle.Launch{Repository: "/repo", Task: "kv", Job: "j1", Spec: core.JobSpec{
		Commit: core.Commit(strings.Repeat("b", 40)), Environment: core.EnvironmentHost,
		Settings: core.CommandSettings{Image: core.ImageHarness, Argv: []string{"true"}, Timeout: time.Minute},
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

// Every end of a wait has its own kind on the wire, and none is UNSPECIFIED, which a client reads
// as no answer.
func TestEveryWaitEndHasItsOwnKind(t *testing.T) {
	ends := []core.WaitEnd{core.WaitAllSucceeded, core.WaitSomeFailed, core.WaitFirstSucceeded, core.WaitFirstFailed, core.WaitTimedOut}
	seen := map[cliv1.WaitOutcomeKind]core.WaitEnd{}
	for _, e := range ends {
		k := wire.WaitOutcome(core.WaitOutcome{End: e}).GetKind()
		if k == cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_UNSPECIFIED {
			t.Fatalf("the end %q goes on the wire as UNSPECIFIED", e)
		}
		if other, ok := seen[k]; ok {
			t.Fatalf("the ends %q and %q share the kind %v", e, other, k)
		}
		seen[k] = e
	}
	if n := len(cliv1.WaitOutcomeKind_name) - 1; n != len(seen) {
		t.Fatalf("the contract has %d outcome kinds and the daemon sends %d", n, len(seen))
	}
	if k := wire.WaitOutcome(core.WaitOutcome{End: "unheard_of"}).GetKind(); k != cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_UNSPECIFIED {
		t.Fatalf("an end the table lacks goes out as %v", k)
	}
}

// A phase this build cannot read is running, which holds, and never one that lets a run go.
func TestAPhaseTheDaemonCannotReadHolds(t *testing.T) {
	want := map[cliv1.SupervisorPhase]lifecycle.RunPhase{
		cliv1.SupervisorPhase_SUPERVISOR_PHASE_UNSPECIFIED: lifecycle.RunRunning,
		cliv1.SupervisorPhase_SUPERVISOR_PHASE_PARKED:      lifecycle.RunParked,
		cliv1.SupervisorPhase_SUPERVISOR_PHASE_RUNNING:     lifecycle.RunRunning,
		cliv1.SupervisorPhase_SUPERVISOR_PHASE_FINISHED:    lifecycle.RunFinished,
		cliv1.SupervisorPhase_SUPERVISOR_PHASE_RELEASED:    lifecycle.RunReleased,
		cliv1.SupervisorPhase(1 << 20):                     lifecycle.RunRunning,
	}
	for p := range cliv1.SupervisorPhase_name {
		if _, ok := want[cliv1.SupervisorPhase(p)]; !ok {
			t.Errorf("phase %d is not covered", p)
		}
	}
	for p, phase := range want {
		if got := wire.ToRunPhase(p); got != phase {
			t.Errorf("ToRunPhase(%v) = %v, want %v", p, got, phase)
		}
	}
}

// A failure a peer sent is read back exactly as sent, a kind this build does not know included,
// so a command can print it unchanged.
func TestAReceivedFailureKeepsAKindThisBuildDoesNotKnow(t *testing.T) {
	for _, kind := range []cliv1.FailureKind{99, cliv1.FailureKind_FAILURE_KIND_CONFLICT, 0} {
		sent := &cliv1.Failure{Kind: kind, Message: "m"}
		detail, err := anypb.New(sent)
		if err != nil {
			t.Fatal(err)
		}
		err = statuspb.FromProto(&status.Status{Code: 9, Message: "m", Details: []*anypb.Any{detail}}).Err()
		got, ok := wire.ContractFailure(err)
		if !ok || got.GetKind() != kind || got.GetMessage() != "m" {
			t.Fatalf("kind %d read back as %v, %v", kind, got, ok)
		}
		if r := wire.ReceivedOf(err); r == nil || r.Failure.GetKind() != kind {
			t.Fatalf("ReceivedOf kind %d = %v", kind, r)
		}
	}
}
