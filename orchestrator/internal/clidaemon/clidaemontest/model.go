package clidaemontest

import (
	"fmt"
	pb "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"google.golang.org/protobuf/types/known/timestamppb"
	"math"
	"testing"
)

type model struct {
	away, recovered bool
	jobs            []*pb.Job
	results         []*pb.Result
	plans           []*pb.Plan
	promotions      []*pb.PromotionRecord
	head            string
}

func newModel() *model              { return &model{} }
func stamp() *timestamppb.Timestamp { return &timestamppb.Timestamp{Seconds: 1767225600} }
func environment() *pb.EnvironmentFingerprint {
	return &pb.EnvironmentFingerprint{Kind: pb.EnvironmentKind_ENVIRONMENT_KIND_HOST, Backend: pb.BackendKind_BACKEND_KIND_CPU, Facts: map[string]string{"go": "1.25"}}
}
func (m *model) play(t *testing.T, op Operation) {
	t.Helper()
	switch op.Op {
	case "measure":
		samples, ok := map[string][]float64{"level": {100, 101, 99, 100, 102}, "win": {120, 121, 119, 122, 120}, "lose": {80, 81, 79, 80, 82}}[op.Samples]
		if !ok {
			t.Fatalf("unknown sample set %s", op.Samples)
		}
		id := fmt.Sprint("j", len(m.jobs)+1)
		key := fmt.Sprint("seed-", len(m.jobs)+1)
		mean, _ := stats(samples)
		m.jobs = append(m.jobs, &pb.Job{Id: id, Key: key, Spec: &pb.JobSpec{Commit: op.Commit, Environment: pb.EnvironmentKind_ENVIRONMENT_KIND_HOST, Settings: &pb.JobSpec_Measure{Measure: &pb.MeasureSettings{Samples: 5}}}, State: pb.JobState_JOB_STATE_SUCCEEDED, Environment: environment(), SubmittedAt: stamp(), StartedAt: stamp(), FinishedAt: stamp(), CheckJobId: id, ResultJobId: id, Submitters: []*pb.Submitter{{At: stamp()}}})
		m.results = append(m.results, &pb.Result{JobId: id, Key: key, Task: "kv-store", TaskVersion: "v1", Commit: op.Commit, SchemaVersion: 1, Environment: environment(), Machine: &pb.MachineFacts{Arch: "amd64", HostId: "host", LogicalCpus: 8, MemoryBytes: 17179869184, Os: "linux"}, Load: &pb.MachineLoad{LoadAverage_1M: 0.25, SampledAt: stamp()}, RecordedAt: stamp(), Outcome: &pb.Result_Measure{Measure: &pb.MeasureResult{Metric: &pb.Metric{Name: "ops", Direction: pb.Direction_DIRECTION_MAXIMIZE}, Samples: samples, Value: mean, CheckJobId: id}}})
	case "create_plan":
		origin := &pb.RecordOrigin{RunId: "run-1", Agent: "agent-1", At: stamp()}
		m.plans = append(m.plans, &pb.Plan{
			Number: uint32(len(m.plans) + 1), Title: op.Title, Body: op.Body, Status: pb.PlanStatus_PLAN_STATUS_OPEN, CreatedAt: stamp(), UpdatedAt: stamp(),
			Origin: origin, Transitions: []*pb.PlanTransition{{Status: pb.PlanStatus_PLAN_STATUS_OPEN, Origin: origin}},
		})
	case "comment_plan":
		if op.Number == 0 || int(op.Number) > len(m.plans) {
			t.Fatal("unknown plan")
		}
		p := m.plans[op.Number-1]
		p.Comments = append(p.Comments, &pb.PlanComment{Text: op.Text, Agent: "agent-1", At: stamp()})
	case "promote":
		previous := m.head
		if previous == "" {
			previous = commitName(0)
		}
		result := m.result(op.Commit)
		base := m.result(previous)
		if result == nil || base == nil {
			t.Fatal("promotion needs measured commits")
		}
		m.promotions = append(m.promotions, &pb.PromotionRecord{Seq: uint64(len(m.promotions) + 1), Promotion: &pb.Promotion{Commit: op.Commit, PreviousHead: previous, Verdict: verdict(result, base), Environment: environment(), ResultJobId: result.JobId, At: stamp()}})
		m.head = op.Commit
	default:
		t.Fatalf("unimplemented corpus operation %q", op.Op)
	}
}
func (m *model) result(commit string) *pb.Result {
	for _, r := range m.results {
		if r.Commit == commit {
			return r
		}
	}
	return nil
}
func stats(v []float64) (float64, float64) {
	mean := 0.0
	for _, x := range v {
		mean += x
	}
	mean /= float64(len(v))
	variance := 0.0
	for _, x := range v {
		variance += (x - mean) * (x - mean)
	}
	return mean, variance / float64(len(v)-1)
}
func verdict(candidate, base *pb.Result) *pb.Verdict {
	c, cv := stats(candidate.GetMeasure().Samples)
	b, bv := stats(base.GetMeasure().Samples)
	ratio := c / b
	noise := math.Sqrt(cv/(5*c*c)+bv/(5*b*b)) * math.Abs(ratio)
	kind := pb.VerdictKind_VERDICT_KIND_WORSE
	if c > b {
		kind = pb.VerdictKind_VERDICT_KIND_BETTER
	}
	return &pb.Verdict{Kind: kind, CandidateSamples: 5, BaseSamples: 5, CandidateMean: c, BaseMean: b, Ratio: ratio, Noise: noise}
}
func (m *model) facts() []*pb.TaskFact {
	head := &pb.Head{State: &pb.Head_None{None: &pb.NoCurrentResult{Reason: pb.NoCurrentResultReason_NO_CURRENT_RESULT_REASON_NO_HEAD}}}
	if m.head != "" {
		head.State = &pb.Head_Current{Current: &pb.CurrentResult{Head: m.head, Result: m.result(m.head)}}
	}
	if m.away {
		return []*pb.TaskFact{{Fact: &pb.TaskFact_Health{Health: &pb.StoreHealth{State: &pb.StoreHealth_Away{Away: &pb.Away{Reason: pb.AwayReason_AWAY_REASON_OWNED_ELSEWHERE, Failure: &pb.Failure{Kind: pb.FailureKind_FAILURE_KIND_CONFLICT, Message: "store owned elsewhere"}}}}}}}
	}
	out := []*pb.TaskFact{{Fact: &pb.TaskFact_Health{Health: &pb.StoreHealth{State: &pb.StoreHealth_Live{Live: &pb.Live{}}}}}, {Fact: &pb.TaskFact_Head{Head: head}}}
	for _, j := range m.jobs {
		out = append(out, &pb.TaskFact{Fact: &pb.TaskFact_Job{Job: j}})
	}
	for _, r := range m.results {
		out = append(out, &pb.TaskFact{Fact: &pb.TaskFact_Result{Result: r}})
	}
	for _, p := range m.promotions {
		out = append(out, &pb.TaskFact{Fact: &pb.TaskFact_Promotion{Promotion: p}})
	}
	for _, p := range m.plans {
		out = append(out, &pb.TaskFact{Fact: &pb.TaskFact_Plan{Plan: p}})
	}
	if m.head != "" {
		for _, r := range m.results {
			if r.Commit != m.head {
				out = append(out, &pb.TaskFact{Fact: &pb.TaskFact_Judgment{Judgment: &pb.Judgment{Commit: r.Commit, Against: m.head, Outcome: &pb.Judgment_Comparison{Comparison: &pb.Comparison{Commit: r.Commit, Against: m.head, Environment: environment(), Verdict: verdict(r, m.result(m.head))}}}}})
			}
		}
	}
	if m.recovered {
		for _, j := range m.jobs {
			if j.State == pb.JobState_JOB_STATE_RUNNING {
				out = append(out, &pb.TaskFact{Fact: &pb.TaskFact_Trouble{Trouble: &pb.JobTrouble{JobId: j.Id, Failure: &pb.Failure{Kind: pb.FailureKind_FAILURE_KIND_UNAVAILABLE, Message: "the store has it running and there is no sign of a run here: if the place that ran it is gone for good, remove the store's owner record and the job is marked lost"}}}})
			}
		}
	}
	return out
}

func commitName(n int) string { return fmt.Sprintf("%c%039d", 'a'+n, 0) }
