package daemon_test

import (
	"fmt"
	"strings"
	"testing"
	"time"

	"buf.build/go/protovalidate"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

func setupCommit(n int) core.Commit { return core.Commit(fmt.Sprintf("%040x", n+1)) }

// putJob records a check or measure job of the task in the store, in the state it names.
func (r *describeRig) putJob(t *testing.T, commit core.Commit, kind core.JobKind, version string, state core.JobState, exit int, stage core.JobStage, at time.Time) {
	t.Helper()
	ctx := ctxFor(t)
	scope, err := core.ResolvedScope("/repo", "t")
	if err != nil {
		t.Fatal(err)
	}
	st, err := r.stores.Create(ctx, scope)
	if err != nil {
		t.Fatal(err)
	}
	var settings core.Settings = core.CheckSettings{}
	if kind == core.JobMeasure {
		settings = core.MeasureSettings{Samples: 2}
	}
	out, err := st.Submit(ctx, core.Submission{
		Context: core.CallContext{Scope: scope, Run: "run-1", Agent: "agent-1"}, Spec: core.JobSpec{Commit: commit, Environment: core.EnvironmentHost, Settings: settings},
		Key: core.JobKey(fmt.Sprintf("key-%d", at.UnixNano())), TaskVersion: version, NoCache: true, At: at,
	})
	if err != nil {
		t.Fatal(err)
	}
	fp := core.Fingerprint{Kind: core.EnvironmentHost, Backend: core.BackendCPU, Facts: map[string]string{"os": "linux"}}
	if _, err := st.Transition(ctx, core.Transition{Job: out.Job.ID, From: core.JobQueued, To: core.JobRunning, At: at, Environment: &fp}); err != nil {
		t.Fatal(err)
	}
	if _, err := st.Transition(ctx, core.Transition{Job: out.Job.ID, From: core.JobRunning, To: state, At: at.Add(time.Second), ExitCode: &exit, FailedStage: stage}); err != nil {
		t.Fatal(err)
	}
}

func describeSetup(r *describeRig, t *testing.T) (*cliv1.DescribeSetupResponse, error) {
	t.Helper()
	return r.client.DescribeSetup(ctxFor(t), &cliv1.DescribeSetupRequest{Repository: "/repo", Task: "t"})
}

// A reading answers the draft's card and declarations, the base, and every job with how its commit
// stands to the base, in a message that meets the contract's rules.
func TestDescribeSetupAnswersTheFactsOfADraft(t *testing.T) {
	r := newDescribeRig(t, true)
	scripted := tasktest.Scripted("t", []string{"true"}, []string{"true"})
	r.files.PutWorkingTree("/repo", scripted)
	// c0 <- c1 (the head) <- c3 (changes code), c4 (changes only the task); c2 is a sibling of c1.
	r.branches.AddCommit("/repo", setupCommit(0))
	r.branches.AddCommit("/repo", setupCommit(1), setupCommit(0))
	r.branches.AddCommit("/repo", setupCommit(2), setupCommit(0))
	r.branches.AddCommit("/repo", setupCommit(3), setupCommit(1))
	r.branches.AddCommit("/repo", setupCommit(4), setupCommit(1))
	r.branches.SetFiles("/repo", setupCommit(1), map[string]string{"a.go": "1"})
	r.branches.SetFiles("/repo", setupCommit(3), map[string]string{"a.go": "2"})
	r.branches.SetFiles("/repo", setupCommit(4), map[string]string{"a.go": "1", task.TasksDir + "/t/x": "1"})
	r.branches.SetHead("/repo", setupCommit(1))
	r.tasks.Put("/repo", setupCommit(1), scripted)
	at := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	for i, j := range []struct {
		commit core.Commit
		stage  core.JobStage
	}{
		{setupCommit(1), core.StageCheck}, {setupCommit(0), ""}, {setupCommit(2), core.StageBuild}, {setupCommit(3), core.StageCheck}, {setupCommit(4), core.StageCheck}, {setupCommit(99), core.StageMeasure},
	} {
		r.putJob(t, j.commit, core.JobCheck, "v", core.JobFailed, 1, j.stage, at.Add(time.Duration(i)*time.Minute))
	}
	resp, err := describeSetup(r, t)
	if err != nil {
		t.Fatal(err)
	}
	if err := protovalidate.Validate(resp); err != nil {
		t.Fatal(err)
	}
	facts := resp.GetFacts()
	if facts.GetTask() != "t" || facts.GetBase().GetCommit() != string(setupCommit(1)) || facts.GetBase().GetTaskVersion() == "" || facts.GetBase().GetTaskVersion() != facts.GetTaskVersion() ||
		facts.GetCard().GetTask() != "t" || !facts.GetDeclarations().GetCheckDeclared() || !facts.GetDeclarations().GetBenchmarkDeclared() || facts.GetJobsTruncated() {
		t.Fatalf("facts = %v", facts)
	}
	type want struct {
		commit   core.Commit
		relation cliv1.SetupCommitRelation
		code     bool
		stage    cliv1.JobStage
	}
	wants := []want{
		{setupCommit(99), cliv1.SetupCommitRelation_SETUP_COMMIT_RELATION_UNKNOWN, false, cliv1.JobStage_JOB_STAGE_MEASURE},
		{setupCommit(4), cliv1.SetupCommitRelation_SETUP_COMMIT_RELATION_DESCENDANT, false, cliv1.JobStage_JOB_STAGE_CHECK},
		{setupCommit(3), cliv1.SetupCommitRelation_SETUP_COMMIT_RELATION_DESCENDANT, true, cliv1.JobStage_JOB_STAGE_CHECK},
		{setupCommit(2), cliv1.SetupCommitRelation_SETUP_COMMIT_RELATION_UNRELATED, false, cliv1.JobStage_JOB_STAGE_BUILD},
		{setupCommit(0), cliv1.SetupCommitRelation_SETUP_COMMIT_RELATION_ANCESTOR, false, cliv1.JobStage_JOB_STAGE_UNSPECIFIED},
		{setupCommit(1), cliv1.SetupCommitRelation_SETUP_COMMIT_RELATION_BASE, false, cliv1.JobStage_JOB_STAGE_CHECK},
	}
	if len(facts.GetJobs()) != len(wants) {
		t.Fatalf("%d jobs, want %d: %v", len(facts.GetJobs()), len(wants), facts.GetJobs())
	}
	for i, w := range wants {
		j := facts.GetJobs()[i]
		if j.GetCommit() != string(w.commit) || j.GetRelation() != w.relation || j.GetCodeChanged() != w.code || j.GetFailedStage() != w.stage || j.GetExitCode() != 1 || j.GetState() != cliv1.JobState_JOB_STATE_FAILED {
			t.Errorf("job %d = %v, want %+v", i, j, w)
		}
	}
}

// A half-written manifest, and a task that is not there, are answers that say why; a request that
// breaks its rules, a daemon with no reader and a repository with no commit are errors.
func TestDescribeSetupSaysWhyADraftHasNoFacts(t *testing.T) {
	r := newDescribeRig(t, true)
	r.branches.AddCommit("/repo", setupCommit(0))
	r.branches.SetHead("/repo", setupCommit(0))
	half := tasktest.Scripted("t", []string{"true"}, []string{"true"})
	half[task.TasksDir+"/t/"+task.ManifestName] = task.File{Data: []byte("version = \"0.2\"\n[accuracy\n")}
	r.files.PutWorkingTree("/repo", half)
	resp, err := describeSetup(r, t)
	if u := resp.GetUnreadable(); err != nil || resp.GetFacts() != nil || u.GetReason() != cliv1.TaskUnreadableReason_TASK_UNREADABLE_REASON_INVALID || !strings.Contains(u.GetDetail(), task.ManifestName) {
		t.Errorf("half-written manifest: %v, %v", resp, err)
	}
	r.files.PutWorkingTree("/repo", task.Tree{})
	if resp, err := describeSetup(r, t); err != nil || resp.GetUnreadable().GetReason() != cliv1.TaskUnreadableReason_TASK_UNREADABLE_REASON_NOT_FOUND {
		t.Errorf("no task: %v, %v", resp, err)
	}
	for name, req := range map[string]*cliv1.DescribeSetupRequest{
		"empty":    {},
		"relative": {Repository: "repo", Task: "t"},
		"no task":  {Repository: "/repo"},
	} {
		if _, err := r.client.DescribeSetup(ctxFor(t), req); status.Code(err) != codes.InvalidArgument {
			t.Errorf("%s: got %v, want InvalidArgument", name, err)
		}
	}
	if _, err := describeSetup(newDescribeRig(t, false), t); status.Code(err) == codes.OK || status.Code(err) == codes.Unimplemented {
		t.Errorf("no reader: got %v, want a refusal", err)
	}
	bare := newDescribeRig(t, true)
	bare.files.PutWorkingTree("/repo", tasktest.Scripted("t", []string{"true"}, []string{"true"}))
	if _, err := describeSetup(bare, t); status.Code(err) != codes.NotFound {
		t.Errorf("a repository with no commit: got %v, want NotFound", err)
	}
}
