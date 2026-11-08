package wire

import (
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/setup"
)

// SetupFactsIn maps the CLI daemon's setup facts to the facts the six checks are derived from. It maps
// and decides nothing: the contract's own rules are the caller's to hold the message to, and a value
// of an enum that has no counterpart here is an error that names it, never a default.
func SetupFactsIn(m *cliv1.SetupFacts) (setup.Facts, error) {
	var errs errorList
	var card core.TaskCard
	if c := cardIn(m.GetCard(), &errs); c != nil {
		card = *c
	} else {
		errs.addf("the setup facts carry no card")
	}
	f := setup.Facts{
		Task: m.GetTask(), Version: m.GetTaskVersion(), Card: card,
		Base: setup.Base{Commit: m.GetBase().GetCommit(), Version: m.GetBase().GetTaskVersion()},
		Decl: setup.Declarations{CheckDeclared: m.GetDeclarations().GetCheckDeclared(), BenchmarkDeclared: m.GetDeclarations().GetBenchmarkDeclared()},
	}
	for _, o := range m.GetDeclarations().GetObjectives() {
		f.Decl.Objectives = append(f.Decl.Objectives, setup.Objective{Name: o.GetName(), Unit: o.GetUnit(), NoiseFloor: copied(o.NoiseFloor)})
	}
	for _, j := range m.GetJobs() {
		job := setup.Job{
			ID: j.GetJobId(), Kind: setupJobKinds.in(j.GetKind(), &errs), State: setupJobStates.in(j.GetState(), &errs),
			Commit: j.GetCommit(), Version: j.GetTaskVersion(), ExitCode: copied(j.ExitCode),
			FailedStage: setupStages.in(j.GetFailedStage(), &errs), Relation: setupRelations.in(j.GetRelation(), &errs),
			CodeChanged: j.GetCodeChanged(), SubmittedAt: j.GetSubmittedAt().AsTime(),
		}
		if j.GetFinishedAt() != nil {
			job.FinishedAt = j.GetFinishedAt().AsTime()
		}
		f.Jobs = append(f.Jobs, job)
	}
	for _, x := range m.GetMeasurements() {
		measure := x.GetMeasure()
		mm := setup.Measurement{
			Commit: x.GetCommit(), Version: x.GetTaskVersion(), Metric: measure.GetMetric().GetName(), Unit: measure.GetMetric().GetUnit(),
			Value: measure.GetValue(), Samples: measure.GetSamples(),
		}
		for _, o := range measure.GetObservations() {
			mm.Observations = append(mm.Observations, setup.Series{Name: o.GetName(), Values: o.GetValues()})
		}
		f.Measurements = append(f.Measurements, mm)
	}
	return f, errs.err()
}

// copied is a pointer to a copy of what p points to, so the facts never share memory with the message
// they were read from.
func copied[T any](p *T) *T {
	if p == nil {
		return nil
	}
	v := *p
	return &v
}

var (
	setupJobKinds = newEnum(map[setup.JobKind]cliv1.JobKind{
		setup.KindCheck:   cliv1.JobKind_JOB_KIND_CHECK,
		setup.KindMeasure: cliv1.JobKind_JOB_KIND_MEASURE,
	})
	setupJobStates = newEnum(map[setup.JobState]cliv1.JobState{
		setup.JobQueued:    cliv1.JobState_JOB_STATE_QUEUED,
		setup.JobRunning:   cliv1.JobState_JOB_STATE_RUNNING,
		setup.JobSucceeded: cliv1.JobState_JOB_STATE_SUCCEEDED,
		setup.JobFailed:    cliv1.JobState_JOB_STATE_FAILED,
		setup.JobCancelled: cliv1.JobState_JOB_STATE_CANCELLED,
		setup.JobLost:      cliv1.JobState_JOB_STATE_LOST,
	})
	setupStages = newEnum(map[setup.Stage]cliv1.JobStage{
		setup.StageUnrecorded: cliv1.JobStage_JOB_STAGE_UNSPECIFIED,
		setup.StageBuild:      cliv1.JobStage_JOB_STAGE_BUILD,
		setup.StageCheck:      cliv1.JobStage_JOB_STAGE_CHECK,
		setup.StageMeasure:    cliv1.JobStage_JOB_STAGE_MEASURE,
	})
	setupRelations = newEnum(map[setup.Relation]cliv1.SetupCommitRelation{
		setup.RelationBase:       cliv1.SetupCommitRelation_SETUP_COMMIT_RELATION_BASE,
		setup.RelationDescendant: cliv1.SetupCommitRelation_SETUP_COMMIT_RELATION_DESCENDANT,
		setup.RelationAncestor:   cliv1.SetupCommitRelation_SETUP_COMMIT_RELATION_ANCESTOR,
		setup.RelationUnrelated:  cliv1.SetupCommitRelation_SETUP_COMMIT_RELATION_UNRELATED,
		setup.RelationUnknown:    cliv1.SetupCommitRelation_SETUP_COMMIT_RELATION_UNKNOWN,
	})
)
