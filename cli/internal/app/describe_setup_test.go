package app_test

import (
	"fmt"
	"math/rand"
	"path"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

// setupWorld is a repository over the fakes of git, of task files and of the store, with a draft
// task in its working tree, that a test fills with commits and jobs.
type setupWorld struct {
	t        *testing.T
	w        *world
	a        *app.App
	files    *task.MemoryCardFiles
	repo     string
	scope    core.Scope
	commits  []core.Commit
	parents  map[core.Commit][]core.Commit
	tree     map[core.Commit]map[string]string
	nextJob  int
	submitAt time.Time
}

const setupTask = "t"

func newSetupWorld(t *testing.T, repo string) *setupWorld {
	t.Helper()
	w := newWorld(t)
	w.branches = gitrepo.NewFakeBranches()
	w.git.AddRepo(repo, nil)
	s := &setupWorld{
		t: t, w: w, files: task.NewMemoryCardFiles(), repo: repo, scope: scopeAt(repo, setupTask),
		parents: map[core.Commit][]core.Commit{}, tree: map[core.Commit]map[string]string{}, submitAt: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC),
	}
	s.files.PutWorkingTree(repo, tasktree(setupTask))
	s.a = w.newApp()
	return s
}

// tasktree is a draft task that declares a check and a benchmark.
func tasktree(name string) task.Tree { return tasktest.Scripted(name, passes, scores) }

func commitID(n int) core.Commit { return core.Commit(fmt.Sprintf("%040x", n+1)) }

// addCommit makes a commit with the parents and the files, labelled by content.
func (s *setupWorld) addCommit(c core.Commit, files map[string]string, parents ...core.Commit) {
	s.w.branches.AddCommit(s.repo, c, parents...)
	s.w.branches.SetFiles(s.repo, c, files)
	s.commits = append(s.commits, c)
	s.parents[c], s.tree[c] = parents, files
}

// addJob submits a check or measure job at the commit, and settles it in the state it names.
func (s *setupWorld) addJob(commit core.Commit, kind core.JobKind, version string, state core.JobState, exit *int, stage core.JobStage) core.JobID {
	s.t.Helper()
	ctx := ctxFor(s.t)
	st, err := s.w.stores.Create(ctx, s.scope)
	if err != nil {
		s.t.Fatal(err)
	}
	var settings core.Settings = core.CheckSettings{}
	if kind == core.JobMeasure {
		settings = core.MeasureSettings{Samples: 2}
	}
	s.nextJob++
	s.submitAt = s.submitAt.Add(time.Minute)
	out, err := st.Submit(ctx, core.Submission{
		Context: core.CallContext{Scope: s.scope, Run: "run-1", Agent: "agent-1"}, Spec: core.JobSpec{Commit: commit, Environment: core.EnvironmentHost, Settings: settings},
		Key: core.JobKey(fmt.Sprintf("key-%d", s.nextJob)), TaskVersion: version, NoCache: true, At: s.submitAt,
	})
	if err != nil {
		s.t.Fatalf("Submit: %v", err)
	}
	id := out.Job.ID
	if state == core.JobQueued {
		return id
	}
	fp := core.Fingerprint{Kind: core.EnvironmentHost, Backend: core.BackendCPU, Facts: map[string]string{"os": "linux"}}
	if _, err := st.Transition(ctx, core.Transition{Job: id, From: core.JobQueued, To: core.JobRunning, At: s.submitAt, Environment: &fp}); err != nil {
		s.t.Fatalf("Transition to running: %v", err)
	}
	if state == core.JobRunning {
		return id
	}
	tr := core.Transition{Job: id, From: core.JobRunning, To: state, At: s.submitAt.Add(time.Second)}
	if state == core.JobFailed {
		tr.ExitCode, tr.FailedStage = exit, stage
	}
	if state == core.JobCancelled {
		tr.CancelReason = "stopped"
	}
	if _, err := st.Transition(ctx, tr); err != nil {
		s.t.Fatalf("Transition to %s: %v", state, err)
	}
	return id
}

func (s *setupWorld) read() app.SetupRead {
	s.t.Helper()
	got, err := s.a.ReadSetup(ctxFor(s.t), app.Caller{Directory: s.repo, Task: setupTask}, s.files)
	if err != nil {
		s.t.Fatalf("ReadSetup: %v", err)
	}
	return got
}

// ancestors is every commit reachable from c, c included, by an independent walk.
func (s *setupWorld) ancestors(c core.Commit) map[core.Commit]bool {
	seen := map[core.Commit]bool{}
	var visit func(core.Commit)
	visit = func(c core.Commit) {
		if seen[c] {
			return
		}
		seen[c] = true
		for _, p := range s.parents[c] {
			visit(p)
		}
	}
	visit(c)
	return seen
}

// relationOracle is how the commit stands to the base, from the definition.
func (s *setupWorld) relationOracle(base, c core.Commit) (app.Relation, bool) {
	if _, ok := s.parents[c]; !ok {
		return app.RelationUnknown, false
	}
	switch {
	case c == base:
		return app.RelationBase, false
	case s.ancestors(c)[base]:
		dir := path.Join(task.TasksDir, setupTask) + "/"
		for _, p := range s.pathsOf(base, c) {
			if !strings.HasPrefix(p, dir) {
				return app.RelationDescendant, true
			}
		}
		return app.RelationDescendant, false
	case s.ancestors(base)[c]:
		return app.RelationAncestor, false
	}
	return app.RelationUnrelated, false
}

func (s *setupWorld) pathsOf(a, b core.Commit) []string {
	var out []string
	for p, l := range s.tree[a] {
		if m, ok := s.tree[b][p]; !ok || m != l {
			out = append(out, p)
		}
	}
	for p := range s.tree[b] {
		if _, ok := s.tree[a][p]; !ok {
			out = append(out, p)
		}
	}
	return out
}

var setupPaths = []string{"a.go", "b.go", "dir/c.go", task.TasksDir + "/" + setupTask + "/x", task.TasksDir + "/" + setupTask + "/y", task.TasksDir + "/other/z"}

// randomHistory fills the world with a random graph of commits, some of them with two parents and
// some roots, each with random files, and returns the head the checkout is at.
func (s *setupWorld) randomHistory(r *rand.Rand) core.Commit {
	n := 2 + r.Intn(10)
	for i := range n {
		files := map[string]string{}
		for _, p := range setupPaths {
			if r.Intn(3) > 0 {
				files[p] = fmt.Sprint(r.Intn(3))
			}
		}
		var parents []core.Commit
		if i > 0 && r.Intn(8) > 0 {
			parents = append(parents, s.commits[r.Intn(i)])
			if r.Intn(4) == 0 {
				if p := s.commits[r.Intn(i)]; p != parents[0] {
					parents = append(parents, p)
				}
			}
		}
		s.addCommit(commitID(i), files, parents...)
	}
	head := s.commits[r.Intn(n)]
	s.w.branches.SetHead(s.repo, head)
	return head
}

func exitCode(n int) *int { return &n }

// For any history and any jobs, a reading says how each job's commit stands to the head by the
// definition of ancestry, flags a change of code only for a descendant that touches a path outside
// the task's directory, and lists the jobs newest first.
func TestReadSetupRelatesEveryJobToTheHead(t *testing.T) {
	for seed := range 60 {
		t.Run(fmt.Sprint(seed), func(t *testing.T) {
			r := rand.New(rand.NewSource(int64(seed)))
			s := newSetupWorld(t, fmt.Sprintf("/repo-%d", seed))
			head := s.randomHistory(r)
			var ids []core.JobID
			commits := slices.Clone(s.commits)
			commits = append(commits, commitID(1000), commitID(1001)) // commits the repository no longer has
			for range r.Intn(30) {
				kind := []core.JobKind{core.JobCheck, core.JobMeasure}[r.Intn(2)]
				state := []core.JobState{core.JobQueued, core.JobRunning, core.JobFailed, core.JobCancelled, core.JobLost}[r.Intn(5)]
				stage := []core.JobStage{"", core.StageBuild, core.StageCheck, core.StageMeasure}[r.Intn(4)]
				var exit *int
				if r.Intn(2) == 0 {
					exit = exitCode(r.Intn(4))
				}
				ids = append(ids, s.addJob(commits[r.Intn(len(commits))], kind, fmt.Sprintf("v%d", r.Intn(2)), state, exit, stage))
			}
			got := s.read()
			if got.Unreadable != nil || got.Base != head {
				t.Fatalf("unreadable = %v, base = %s, want the head %s", got.Unreadable, got.Base, head)
			}
			if len(got.Jobs) != len(ids) || got.JobsTruncated {
				t.Fatalf("%d jobs, truncated %v; want %d", len(got.Jobs), got.JobsTruncated, len(ids))
			}
			for i, j := range got.Jobs {
				if want := ids[len(ids)-1-i]; j.Job.ID != want {
					t.Fatalf("job %d is %s, want %s: newest submitted first", i, j.Job.ID, want)
				}
				relation, code := s.relationOracle(head, j.Job.Spec.Commit)
				if j.Relation != relation || j.CodeChanged != code {
					t.Errorf("job at %.7s: relation %d, code changed %v; want %d, %v", j.Job.Spec.Commit, j.Relation, j.CodeChanged, relation, code)
				}
				if j.CodeChanged && j.Relation != app.RelationDescendant {
					t.Errorf("job at %.7s changed code but is not a descendant", j.Job.Spec.Commit)
				}
			}
		})
	}
}

// More jobs than a reading holds keep the newest, and say that they were cut.
func TestReadSetupKeepsTheNewestJobsWhenThereAreTooMany(t *testing.T) {
	s := newSetupWorld(t, "/repo-many")
	s.addCommit(commitID(0), nil)
	s.w.branches.SetHead(s.repo, commitID(0))
	var ids []core.JobID
	for range app.MaxSetupJobs + 7 {
		ids = append(ids, s.addJob(commitID(0), core.JobCheck, "v", core.JobFailed, exitCode(1), core.StageCheck))
	}
	got := s.read()
	if len(got.Jobs) != app.MaxSetupJobs || !got.JobsTruncated {
		t.Fatalf("%d jobs, truncated %v; want %d, true", len(got.Jobs), got.JobsTruncated, app.MaxSetupJobs)
	}
	for i, j := range got.Jobs {
		if want := ids[len(ids)-1-i]; j.Job.ID != want {
			t.Fatalf("job %d is %s, want the %dth newest, %s", i, j.Job.ID, i+1, want)
		}
	}
	exact := newSetupWorld(t, "/repo-exact")
	exact.addCommit(commitID(0), nil)
	exact.w.branches.SetHead(exact.repo, commitID(0))
	for range app.MaxSetupJobs {
		exact.addJob(commitID(0), core.JobCheck, "v", core.JobQueued, nil, "")
	}
	if got := exact.read(); len(got.Jobs) != app.MaxSetupJobs || got.JobsTruncated {
		t.Errorf("exactly %d jobs: got %d, truncated %v", app.MaxSetupJobs, len(got.Jobs), got.JobsTruncated)
	}
}

// Only check and measure jobs are facts, the recorded stage and exit code travel with the job, and
// the version of the working tree is the draft's, not any job's.
func TestReadSetupReadsTheDraftAndTheStageOfAFailedJob(t *testing.T) {
	s := newSetupWorld(t, "/repo-draft")
	s.addCommit(commitID(0), nil)
	s.w.branches.SetHead(s.repo, commitID(0))
	s.w.tasks.Put(s.repo, commitID(0), tasktree(setupTask))
	ctx := ctxFor(t)
	st, err := s.w.stores.Create(ctx, s.scope)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := st.Submit(ctx, core.Submission{
		Context: core.CallContext{Scope: s.scope, Run: "run-1"}, Spec: core.JobSpec{Commit: commitID(0), Environment: core.EnvironmentHost, Settings: core.CommandSettings{Image: core.ImageHarness, Argv: []string{"true"}}},
		At: s.submitAt,
	}); err != nil {
		t.Fatal(err)
	}
	id := s.addJob(commitID(0), core.JobCheck, "v", core.JobFailed, exitCode(3), core.StageCheck)
	got := s.read()
	if len(got.Jobs) != 1 || got.Jobs[0].Job.ID != id || got.Jobs[0].Job.FailedStage != core.StageCheck || got.Jobs[0].Job.ExitCode == nil || *got.Jobs[0].Job.ExitCode != 3 {
		t.Fatalf("jobs = %+v, want the one check job with its stage and exit code", got.Jobs)
	}
	tk, err := s.w.tasks.Task(ctx, s.repo, commitID(0), setupTask)
	if err != nil {
		t.Fatal(err)
	}
	if got.Draft.Version == "" || got.Draft.Version != tk.Version || got.BaseVersion != tk.Version {
		t.Errorf("draft version %q, base version %q, want both %q", got.Draft.Version, got.BaseVersion, tk.Version)
	}
	if d := got.Draft.Declarations; !d.GetCheckDeclared() || !d.GetBenchmarkDeclared() || len(d.GetObjectives()) != 1 {
		t.Errorf("declarations = %v", d)
	}
	// An edit to the working tree moves the draft's version and not the base's.
	edited := tasktree(setupTask)
	edited[task.TasksDir+"/"+setupTask+"/helper.sh"] = task.File{Data: []byte("echo\n")}
	s.files.PutWorkingTree(s.repo, edited)
	if again := s.read(); again.Draft.Version == got.Draft.Version || again.BaseVersion != got.BaseVersion {
		t.Errorf("after an edit: draft version %q (was %q), base version %q (was %q)", again.Draft.Version, got.Draft.Version, again.BaseVersion, got.BaseVersion)
	}
}

// A draft whose manifest is half written, or that is not there, has no facts and no git or store
// was read for it; a task that is committed under no other form still has a base.
func TestReadSetupSaysWhyADraftHasNoFacts(t *testing.T) {
	s := newSetupWorld(t, "/repo-half")
	s.addCommit(commitID(0), nil)
	s.w.branches.SetHead(s.repo, commitID(0))
	half := tasktree(setupTask)
	half[task.TasksDir+"/"+setupTask+"/"+task.ManifestName] = task.File{Data: []byte("version = \"0.2\"\n[accuracy\n")}
	s.files.PutWorkingTree(s.repo, half)
	if got := s.read(); got.Unreadable == nil || got.Unreadable.Kind != core.FailureInvalidInput || !strings.Contains(got.Unreadable.Message, task.ManifestName) || len(got.Jobs) != 0 {
		t.Errorf("a half-written manifest: %+v", got)
	}
	s.files.PutWorkingTree(s.repo, task.Tree{})
	if got := s.read(); got.Unreadable == nil || got.Unreadable.Kind != core.FailureNotFound {
		t.Errorf("no task: %+v", got)
	}
}
