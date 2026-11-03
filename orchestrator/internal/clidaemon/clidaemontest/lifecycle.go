package clidaemontest

import (
	"fmt"
	pb "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"google.golang.org/protobuf/types/known/durationpb"
	"testing"
)

func (m *model) operation(t *testing.T, op Operation, lifecycle bool) {
	if lifecycle {
		m.lifecycle(t, op)
	} else {
		m.play(t, op)
	}
}

// lifecycle models durable jobs, attribution and recovery independently of the
// daemon implementation. Reopening preserves the store, losing its workers.
func (m *model) lifecycle(t *testing.T, op Operation) {
	t.Helper()
	switch op.Op {
	case "submit":
		var job *pb.Job
		for _, j := range m.jobs {
			if j.Key == "corpus-check" {
				job = j
				break
			}
		}
		if job == nil {
			job = &pb.Job{Id: fmt.Sprint("j", len(m.jobs)+1), Key: "corpus-check", State: pb.JobState_JOB_STATE_QUEUED, SubmittedAt: stamp(), Spec: &pb.JobSpec{Commit: op.Commit, Environment: pb.EnvironmentKind_ENVIRONMENT_KIND_HOST, Settings: &pb.JobSpec_Check{Check: &pb.CheckSettings{}}}}
			m.jobs = append(m.jobs, job)
		}
		for _, s := range job.Submitters {
			if s.RunId == op.Run && s.Agent == op.Agent {
				return
			}
		}
		job.Submitters = append(job.Submitters, &pb.Submitter{RunId: op.Run, Agent: op.Agent, At: stamp()})
	case "start":
		j := m.job(t, op.Job)
		j.State = pb.JobState_JOB_STATE_RUNNING
		j.StartedAt = stamp()
		j.Environment = environment()
	case "finish":
		j := m.job(t, op.Job)
		j.State = pb.JobState_JOB_STATE_SUCCEEDED
		j.FinishedAt = stamp()
		j.ResultJobId = j.Id
		m.results = append(m.results, &pb.Result{JobId: j.Id, Key: j.Key, Task: "task", TaskVersion: "v1", Commit: j.Spec.Commit, SchemaVersion: 1, Environment: environment(), Machine: &pb.MachineFacts{}, Load: &pb.MachineLoad{}, RecordedAt: stamp(), Outcome: &pb.Result_Check{Check: &pb.CheckResult{Duration: &durationpb.Duration{Seconds: 1}}}})
	case "cancel":
		j := m.job(t, op.Job)
		j.State = pb.JobState_JOB_STATE_CANCELLED
		j.FinishedAt = stamp()
		j.CancelReason = "cancelled by corpus"
	case "owner_loss":
		m.away = true
	case "owner_return":
		m.away = false
	case "crash_reopen":
		m.recovered = true
	case "forget":
		*m = *newModel()
	case "create_plans":
		for i := uint32(0); i < op.Number; i++ {
			m.play(t, Operation{Op: "create_plan", Title: op.Title})
		}
	default:
		t.Fatalf("unknown lifecycle operation %q", op.Op)
	}
}
func (m *model) job(t *testing.T, id string) *pb.Job {
	t.Helper()
	for _, j := range m.jobs {
		if j.Id == id {
			return j
		}
	}
	t.Fatalf("unknown job %q", id)
	return nil
}
