package clientcli_test

import (
	"context"
	"math/rand/v2"
	"strings"
	"testing"
	"testing/fstest"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// The service names a run's repository with its links resolved. A RUN left out finds the live run of
// the repository whichever name the working directory has, through any chain of links, and never
// finds a run of another directory that merely has a similar name.
func TestAnOmittedRunIsFoundThroughAnyLinkToItsRepository(t *testing.T) {
	for seed := range uint64(60) {
		r := rand.New(rand.NewPCG(seed, 5))
		names := []string{"/real/repo", "/work/repo", "/home/me/link", "/srv/a/b/link"}
		r.Shuffle(len(names), func(i, j int) { names[i], names[j] = names[j], names[i] })
		// Each name but the first links to the one before it, so the chain ends at names[0].
		links := map[string]string{}
		files := fstest.MapFS{}
		for i, name := range names {
			files[strings.TrimPrefix(name, "/")+"/.git"] = &fstest.MapFile{}
			if i > 0 {
				links[name] = names[i-1]
			}
		}
		runs := []*pb.Run{
			runOf("run-here", names[0], pb.RunStatus_RUN_STATUS_RUNNING, 2),
			runOf("run-other", "/real/repo-other", pb.RunStatus_RUN_STATUS_RUNNING, 1),
		}
		f := &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(1, false, runs, nil)}}
		w := newWorld(t, serveFake(t, f))
		w.files, w.links = files, links
		w.dir = names[r.IntN(len(names))]
		got := w.run(context.Background(), "run", "get", "--json")
		if got.code != 0 || !strings.Contains(got.stdout, `"run-here"`) || strings.Contains(got.stdout, "run-other") {
			t.Fatalf("seed %d: from %s (links %v) exit %d, stdout %q, stderr %q", seed, w.dir, links, got.code, got.stdout, got.stderr)
		}
	}
}
