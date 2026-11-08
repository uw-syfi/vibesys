package daemon

import (
	"strings"
	"unicode/utf8"

	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// maxUnreadableDetail is the contract's bound on the words of TaskUnreadable.
const maxUnreadableDetail = 2048

var setupRelations = map[app.Relation]cliv1.SetupCommitRelation{
	app.RelationBase:       cliv1.SetupCommitRelation_SETUP_COMMIT_RELATION_BASE,
	app.RelationDescendant: cliv1.SetupCommitRelation_SETUP_COMMIT_RELATION_DESCENDANT,
	app.RelationAncestor:   cliv1.SetupCommitRelation_SETUP_COMMIT_RELATION_ANCESTOR,
	app.RelationUnrelated:  cliv1.SetupCommitRelation_SETUP_COMMIT_RELATION_UNRELATED,
	app.RelationUnknown:    cliv1.SetupCommitRelation_SETUP_COMMIT_RELATION_UNKNOWN,
}

// setupResponse is the contract's answer for a reading of the setup facts of the named task.
func setupResponse(name string, r app.SetupRead) *cliv1.DescribeSetupResponse {
	if r.Unreadable != nil {
		return &cliv1.DescribeSetupResponse{Outcome: &cliv1.DescribeSetupResponse_Unreadable{Unreadable: unreadable(*r.Unreadable)}}
	}
	facts := &cliv1.SetupFacts{
		Task:          name,
		TaskVersion:   r.Draft.Version,
		Card:          r.Draft.Card,
		Declarations:  r.Draft.Declarations,
		Base:          &cliv1.SetupBase{Commit: string(r.Base), TaskVersion: r.BaseVersion},
		Measurements:  wire.Measurements(r.Measurements),
		JobsTruncated: r.JobsTruncated,
	}
	for _, j := range r.Jobs {
		facts.Jobs = append(facts.Jobs, setupJob(j))
	}
	return &cliv1.DescribeSetupResponse{Outcome: &cliv1.DescribeSetupResponse_Facts{Facts: facts}}
}

func setupJob(j app.SetupJob) *cliv1.SetupJob {
	out := &cliv1.SetupJob{
		JobId:       string(j.Job.ID),
		Kind:        wire.JobKind(j.Job.Spec.Settings.Kind()),
		State:       wire.JobState(j.Job.State),
		Commit:      string(j.Job.Spec.Commit),
		TaskVersion: j.Job.TaskVersion,
		Relation:    setupRelations[j.Relation],
		CodeChanged: j.CodeChanged,
		SubmittedAt: timestamppb.New(j.Job.SubmittedAt),
		FailedStage: wire.JobStage(j.Job.FailedStage),
	}
	if j.Job.FinishedAt != nil {
		out.FinishedAt = timestamppb.New(*j.Job.FinishedAt)
	}
	if j.Job.ExitCode != nil {
		code := int32(*j.Job.ExitCode)
		out.ExitCode = &code
	}
	return out
}

// unreadable is the contract's account of a draft that has no facts yet: a task or manifest that is
// not there, or a file that is invalid, in the reader's words.
func unreadable(f core.Failure) *cliv1.TaskUnreadable {
	reason := cliv1.TaskUnreadableReason_TASK_UNREADABLE_REASON_INVALID
	if f.Kind == core.FailureNotFound {
		reason = cliv1.TaskUnreadableReason_TASK_UNREADABLE_REASON_NOT_FOUND
	}
	detail := strings.ToValidUTF8(f.Message, "?")
	if len(detail) > maxUnreadableDetail {
		detail = detail[:maxUnreadableDetail]
		for !utf8.ValidString(detail) {
			detail = detail[:len(detail)-1]
		}
	}
	if detail == "" {
		detail = "the task's files cannot be read"
	}
	return &cliv1.TaskUnreadable{Reason: reason, Detail: detail}
}
