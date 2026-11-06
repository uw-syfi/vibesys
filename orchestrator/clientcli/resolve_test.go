package clientcli_test

import (
	"context"
	"fmt"
	"math/rand/v2"
	"regexp"
	"strings"
	"testing"
	"testing/fstest"

	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

var repositories = []string{"/work/repo", "/work/other", "/srv/third"}

var allStatuses = []pb.RunStatus{
	pb.RunStatus_RUN_STATUS_STARTING, pb.RunStatus_RUN_STATUS_RUNNING, pb.RunStatus_RUN_STATUS_STOPPING,
	pb.RunStatus_RUN_STATUS_ENDED, pb.RunStatus_RUN_STATUS_FAILED,
}

func isLive(r *pb.Run) bool {
	return r.GetStatus() != pb.RunStatus_RUN_STATUS_ENDED && r.GetStatus() != pb.RunStatus_RUN_STATUS_FAILED
}

// randomRuns makes runs with ids that share prefixes often enough to be ambiguous.
func randomRuns(r *rand.Rand) []*pb.Run {
	var runs []*pb.Run
	seen := map[string]bool{}
	for range 1 + r.IntN(7) {
		id := fmt.Sprintf("%c%c%03d", 'a'+r.IntN(2), 'a'+r.IntN(2), r.IntN(30))
		if seen[id] {
			continue
		}
		seen[id] = true
		runs = append(runs, runOf(id, repositories[r.IntN(len(repositories))], allStatuses[r.IntN(len(allStatuses))], r.IntN(100)))
	}
	return runs
}

// get resolves arg against runs by asking `run get --json`, and returns the id it chose.
func getID(t *testing.T, runs []*pb.Run, args ...string) (id string, got result) {
	t.Helper()
	f := &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(1, false, runs, nil)}}
	w := newWorld(t, serveFake(t, f))
	got = w.run(context.Background(), append([]string{"run", "get", "--json"}, args...)...)
	if got.code == 0 {
		var snapshot pb.RunsSnapshot
		if err := protojson.Unmarshal([]byte(got.stdout), &snapshot); err != nil || len(snapshot.GetRuns()) != 1 {
			t.Fatalf("stdout %q: %v", got.stdout, err)
		}
		id = snapshot.GetRuns()[0].GetId()
	}
	return id, got
}

// RUN is a full id, a unique prefix, or omitted, over any set of runs: a full id always finds its
// run; a prefix finds the one run it starts, and otherwise fails naming every run it starts; and
// no argument finds the one live run of this repository, or with none its most recent run, and
// otherwise fails naming the choices.
func TestRunResolutionOverRandomRunSets(t *testing.T) {
	r := rand.New(rand.NewPCG(7, 11))
	for i := range 120 {
		runs := randomRuns(r)
		t.Run(fmt.Sprintf("set-%d", i), func(t *testing.T) {
			target := runs[r.IntN(len(runs))]

			if id, got := getID(t, runs, target.GetId()); got.code != 0 || id != target.GetId() {
				t.Errorf("full id %q resolved to %q (exit %d, %q)", target.GetId(), id, got.code, got.stderr)
			}

			prefix := target.GetId()[:1+r.IntN(len(target.GetId()))]
			var starts []string
			for _, run := range runs {
				if strings.HasPrefix(run.GetId(), prefix) {
					starts = append(starts, run.GetId())
				}
			}
			id, got := getID(t, runs, prefix)
			if exact := containsString(starts, prefix); len(starts) == 1 || exact {
				want := starts[0]
				if exact {
					want = prefix
				}
				if got.code != 0 || id != want {
					t.Errorf("prefix %q of %v resolved to %q (exit %d, %q), want %q", prefix, starts, id, got.code, got.stderr, want)
				}
			} else {
				if got.code != 1 {
					t.Errorf("ambiguous prefix %q of %v: exit %d", prefix, starts, got.code)
				}
				for _, s := range starts {
					contains(t, "stderr", got.stderr, s)
				}
			}

			var here, elsewhere []string
			var endedHere []*pb.Run
			for _, run := range runs {
				switch {
				case !isLive(run) && run.GetRepository() == "/work/repo":
					endedHere = append(endedHere, run)
				case !isLive(run):
				case run.GetRepository() == "/work/repo":
					here = append(here, run.GetId())
				default:
					elsewhere = append(elsewhere, run.GetId())
				}
			}
			id, got = getID(t, runs)
			switch len(here) {
			case 1:
				if got.code != 0 || id != here[0] {
					t.Errorf("omitted RUN resolved to %q (exit %d, %q), want %q", id, got.code, got.stderr, here[0])
				}
			case 0:
				if len(endedHere) > 0 {
					newest := endedHere[0].GetCreatedAt().AsTime()
					for _, run := range endedHere {
						if run.GetCreatedAt().AsTime().After(newest) {
							newest = run.GetCreatedAt().AsTime()
						}
					}
					var chosen *pb.Run
					for _, run := range endedHere {
						if run.GetId() == id {
							chosen = run
						}
					}
					if got.code != 0 || chosen == nil || !chosen.GetCreatedAt().AsTime().Equal(newest) {
						t.Errorf("no live run here, %d ended: resolved to %q (exit %d, %q), want one from %v", len(endedHere), id, got.code, got.stderr, newest)
					}
					break
				}
				if got.code != 1 {
					t.Errorf("no live run here: exit %d", got.code)
				}
				for _, s := range elsewhere {
					contains(t, "stderr", got.stderr, s)
				}
			default:
				if got.code != 1 {
					t.Errorf("%d live runs here: exit %d", len(here), got.code)
				}
				for _, s := range here {
					contains(t, "stderr", got.stderr, s)
				}
			}
		})
	}
}

func containsString(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

// Run resolution of an omitted RUN works from any directory inside the repository, and never
// from one that is not in a repository.
func TestAnOmittedRunIsFoundFromASubdirectory(t *testing.T) {
	runs := []*pb.Run{
		runOf("run-here", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 1),
		runOf("run-there", "/work/other", pb.RunStatus_RUN_STATUS_RUNNING, 2),
	}
	for _, dir := range []string{"/work/repo", "/work/repo/a", "/work/repo/a/b/c"} {
		f := &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(1, false, runs, nil)}}
		w := newWorld(t, serveFake(t, f))
		w.dir = dir
		got := w.run(context.Background(), "run", "get", "--json")
		if got.code != 0 || !strings.Contains(got.stdout, "run-here") || strings.Contains(got.stdout, "run-there") {
			t.Errorf("from %s: exit %d, stdout %q, stderr %q", dir, got.code, got.stdout, got.stderr)
		}
	}
	w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(1, false, runs, nil)}}))
	w.dir = "/tmp/not-a-repo"
	got := w.run(context.Background(), "run", "get")
	if got.code != 2 {
		t.Fatalf("outside a repository: exit %d, want 2", got.code)
	}
	contains(t, "stderr", got.stderr, "/tmp/not-a-repo", "git repository")
}

// The repository of a new run is the git root of the working directory, found by walking up for a
// .git that may be a file or a directory; a given one is made absolute.
func TestStartsRunInTheGitRootOfTheWorkingDirectory(t *testing.T) {
	dirEntry := &fstest.MapFile{Mode: 0o20000000755}
	cases := []struct {
		name  string
		files fstest.MapFS
		dir   string
		args  []string
		want  string
	}{
		{"git file at the root", fstest.MapFS{"work/repo/.git": {}}, "/work/repo/pkg/deep", nil, "/work/repo"},
		{"git directory", fstest.MapFS{"work/repo/.git": dirEntry, "work/repo/.git/HEAD": {}}, "/work/repo", nil, "/work/repo"},
		{"the nearest .git wins", fstest.MapFS{"work/.git": {}, "work/repo/sub/.git": {}}, "/work/repo/sub/x", nil, "/work/repo/sub"},
		{"filesystem root", fstest.MapFS{".git": {}}, "/a/b", nil, "/"},
		{"a flag is made absolute", fstest.MapFS{}, "/not/a/repo", []string{"--repository", "../x/./y"}, "/not/a/x/y"},
		{"an absolute flag is cleaned", fstest.MapFS{}, "/not/a/repo", []string{"--repository", "/r//s/"}, "/r/s"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := &fakeService{dispatch: refuseAll}
			w := newWorld(t, serveFake(t, f))
			w.files, w.dir = tc.files, tc.dir
			w.run(context.Background(), append([]string{"run", "start", "--task", "toy"}, tc.args...)...)
			sent := f.sent()
			if len(sent) != 1 || sent[0].GetStartRun().GetRepository() != tc.want {
				t.Errorf("sent %v, want repository %q", sent, tc.want)
			}
		})
	}
}

func refuseAll(cmd *pb.Command) (*pb.DispatchResponse, error) {
	return &pb.DispatchResponse{Receipt: &pb.Receipt{CommandId: cmd.GetId(), Refusal: pb.Refusal_REFUSAL_RUN_NOT_FOUND}}, nil
}

func TestStartOutsideAGitRepositoryIsAUsageErrorNamingTheDirectory(t *testing.T) {
	f := &fakeService{dispatch: refuseAll}
	w := newWorld(t, serveFake(t, f))
	w.dir = "/tmp/scratch/deep"
	got := w.run(context.Background(), "run", "start", "--task", "toy")
	if got.code != 2 {
		t.Fatalf("exit %d, want 2", got.code)
	}
	contains(t, "stderr", got.stderr, "/tmp/scratch/deep", "not in a git repository", "--repository")
	if len(f.sent()) != 0 {
		t.Errorf("sent %v", f.sent())
	}
}

// endedHere is a world whose repository's runs have all ended, the older one and the newer one, with
// a live run elsewhere that an omitted RUN must never mean.
func endedHere(t *testing.T) (*fakeService, *world) {
	t.Helper()
	runs := []*pb.Run{
		runOf("run-older", "/work/repo", pb.RunStatus_RUN_STATUS_FAILED, 1),
		runOf("run-newer", "/work/repo", pb.RunStatus_RUN_STATUS_ENDED, 5),
		runOf("run-there", "/work/other", pb.RunStatus_RUN_STATUS_RUNNING, 9),
	}
	f := &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(1, false, runs, nil)}}
	return f, newWorld(t, serveFake(t, f))
}

// Finding: after a run ended the installer's next steps failed, with no candidates. run get shows the
// repository's most recent run.
func TestRunGetWithNoRunShowsTheMostRecentRunHere(t *testing.T) {
	_, w := endedHere(t)
	got := w.run(context.Background(), "run", "get", "--json")
	if got.code != 0 || !strings.Contains(got.stdout, "run-newer") || strings.Contains(got.stdout, "run-there") {
		t.Fatalf("exit %d: %s%s", got.code, got.stdout, got.stderr)
	}
}

// run stop of a run that has ended says so, sends nothing and exits 0, however the run was named.
func TestRunStopOfARunThatHasEndedIsAlreadyDone(t *testing.T) {
	for _, args := range [][]string{{"run", "stop", "--yes"}, {"run", "stop"}, {"run", "stop", "run-newer", "--yes"}, {"run", "stop", "run-older"}} {
		f, w := endedHere(t)
		w.terminal = true
		got := w.run(context.Background(), args...)
		if got.code != 0 || !strings.Contains(got.stdout, "already") || len(f.sent()) != 0 {
			t.Errorf("%v: exit %d, sent %v: %s%s", args, got.code, f.sent(), got.stdout, got.stderr)
		}
	}
}

// run attach replays the most recent run and returns. It says the run has ended, and promises no detach.
func TestRunAttachWithNoRunReplaysTheMostRecentRunHere(t *testing.T) {
	_, w := endedHere(t)
	got := w.run(context.Background(), "run", "attach")
	if got.code != 0 || !strings.Contains(got.stderr, "run-newer") || strings.Contains(got.stderr, "Ctrl-C") {
		t.Fatalf("exit %d: %s%s", got.code, got.stdout, got.stderr)
	}
}

// run steer to a run that has ended is an error: the message would be lost. It says so, sends nothing,
// and is the same whether the run is named or found.
func TestSteerOfARunThatHasEndedIsRefusedPlainly(t *testing.T) {
	for _, args := range [][]string{{"run", "steer", "hello"}, {"run", "steer", "run-newer", "hello"}} {
		f, w := endedHere(t)
		got := w.run(context.Background(), args...)
		if got.code != 1 || len(f.sent()) != 0 {
			t.Errorf("%v: exit %d, %d commands sent", args, got.code, len(f.sent()))
		}
		contains(t, "stderr", got.stderr, "run-newer", "already ended", "not sent")
	}
}

// Finding: leaving a run and coming back after it ended, run wait with no RUN said "no live run". It
// now returns at once, as wait for the same RUN does, for every --until.
func TestWaitWithNoRunOnARunThatEndedIsWaitForThatRun(t *testing.T) {
	for until := range goalNames {
		var ids [2]string
		var codes [2]int
		for i, name := range []string{"", "run-newer"} {
			_, w := endedHere(t)
			args := []string{"run", "wait", "--until", until, "--timeout", "1m"}
			if name != "" {
				args = append(args, name)
			}
			got := w.run(context.Background(), args...)
			codes[i], ids[i] = got.code, waitedRun(got.stderr)
		}
		if ids[0] != "run-newer" || ids[1] != "run-newer" || codes[0] != codes[1] || codes[0] == 3 || codes[0] == 4 {
			t.Errorf("--until %s: no RUN gave run %q exit %d, RUN given gave run %q exit %d", until, ids[0], codes[0], ids[1], codes[1])
		}
	}
}

// With no run ever started in the repository, the error names run start.
func TestAVerbWithNoRunInTheRepositoryNamesRunStart(t *testing.T) {
	for _, args := range [][]string{{"run", "get"}, {"run", "steer", "hi"}, {"run", "stop"}, {"run", "wait", "--until", "ended", "--timeout", "1m"}} {
		runs := []*pb.Run{runOf("run-there", "/work/other", pb.RunStatus_RUN_STATUS_RUNNING, 9)}
		w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(1, false, runs, nil)}}))
		got := w.run(context.Background(), args...)
		if got.code != 1 {
			t.Errorf("%v: exit %d", args, got.code)
		}
		contains(t, "stderr", got.stderr, "run start", "run-there")
	}
}

var goalNames = map[string]bool{"started": true, "report": true, "progress": true, "stalled": true, "ended": true}

var waitedRunLine = regexp.MustCompile(`on run (\S+) \(`)

// waitedRun is the run a wait says it is waiting on.
func waitedRun(stderr string) string {
	if m := waitedRunLine.FindStringSubmatch(stderr); m != nil {
		return m[1]
	}
	return ""
}

var stoppedRunLine = regexp.MustCompile(`run (\S+) has already|stopping run (\S+);`)

// run get, run wait and run stop with no RUN mean one run, over a repository with no runs, one live
// run, one ended, several ended, and live with ended, with runs elsewhere mixed in.
func TestOmittedRunMeansTheSameRunForGetWaitAndStop(t *testing.T) {
	r := rand.New(rand.NewPCG(3, 5))
	for i := range 80 {
		var runs []*pb.Run
		var agents []*pb.Agent
		add := func(repo string, status pb.RunStatus) {
			id := fmt.Sprintf("run-%d", len(runs))
			runs = append(runs, runOf(id, repo, status, len(runs)))
			if status == pb.RunStatus_RUN_STATUS_RUNNING {
				agents = append(agents, leadOf(id, pb.AgentStatus_AGENT_STATUS_IDLE))
			}
		}
		for range r.IntN(4) { // ended here: none, one, several
			add("/work/repo", allStatuses[3+r.IntN(2)])
		}
		if r.IntN(2) == 0 { // one live here
			add("/work/repo", pb.RunStatus_RUN_STATUS_RUNNING)
		}
		for range r.IntN(3) { // other repositories
			add("/work/other", allStatuses[r.IntN(len(allStatuses))])
		}
		r.Shuffle(len(runs), func(a, b int) { runs[a], runs[b] = runs[b], runs[a] })
		t.Run(fmt.Sprintf("set-%d", i), func(t *testing.T) {
			ask := func(args ...string) result {
				w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(1, false, runs, agents)}}))
				return w.run(context.Background(), args...)
			}
			gotGet := ask("run", "get", "--json")
			if gotGet.code != 0 {
				// No run here, or none to choose: wait and stop must fail the same way, naming the candidates.
				for _, args := range [][]string{{"run", "stop", "--yes"}, {"run", "wait", "--until", "started", "--timeout", "1m"}} {
					if got := ask(args...); got.code != 1 || got.stderr != gotGet.stderr {
						t.Errorf("%v: exit %d, %q; get said %q", args, got.code, got.stderr, gotGet.stderr)
					}
				}
				return
			}
			var snapshot pb.RunsSnapshot
			if err := protojson.Unmarshal([]byte(gotGet.stdout), &snapshot); err != nil || len(snapshot.GetRuns()) != 1 {
				t.Fatalf("get: %q: %v", gotGet.stdout, err)
			}
			want := snapshot.GetRuns()[0].GetId()
			if got := ask("run", "wait", "--until", "started", "--timeout", "1m"); waitedRun(got.stderr) != want {
				t.Errorf("wait chose %q (exit %d, %q), get chose %q", waitedRun(got.stderr), got.code, got.stderr, want)
			}
			got := ask("run", "stop", "--yes")
			m := stoppedRunLine.FindStringSubmatch(got.stdout)
			if got.code != 0 || m == nil || m[1]+m[2] != want {
				t.Errorf("stop chose %v (exit %d, %q), get chose %q", m, got.code, got.stdout, want)
			}
		})
	}
}

// --help says all of this.
func TestHelpStatesWhatAnOmittedRunMeans(t *testing.T) {
	_, w := endedHere(t)
	got := w.run(context.Background(), "--help")
	contains(t, "stdout", got.stdout, "most recent run", "already ended", "replays")
}
