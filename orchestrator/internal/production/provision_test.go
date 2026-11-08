package production

import (
	"context"
	"fmt"
	"io/fs"
	"maps"
	"math/rand/v2"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"testing/fstest"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

const (
	reposDir = "/home/user/.vibesys/repos"
	homeDir  = "/home/user"
)

// world is a Provisioner over a memFS and the Fake git, the Fake tree source and the faults that
// break them.
type provisionWorld struct {
	faults *faults
	fs     *memFS
	git    *fakeGit
	trees  *daemontest.FakeExampleTrees
	p      *Provisioner
}

func newProvisionWorld() *provisionWorld {
	f := newFaults()
	m := newMemFS(f)
	g := &fakeGit{fs: m, remotes: map[string]map[string]map[string]string{}}
	trees := &daemontest.FakeExampleTrees{}
	return &provisionWorld{faults: f, fs: m, git: g, trees: trees, p: &Provisioner{Trees: trees, FS: m, Git: g, Default: "~/.vibesys/repos", Home: homeDir}}
}

func exampleReq(id, name, example, parent string) *pb.ProvisionRepositoryRequest {
	return &pb.ProvisionRepositoryRequest{
		RequestId: id, Name: name, ParentDirectory: parent,
		Source: &pb.ProvisionRepositoryRequest_Example{Example: example},
	}
}

func (w *provisionWorld) provision(t *testing.T, req *pb.ProvisionRepositoryRequest) *pb.ProvisionRepositoryResponse {
	t.Helper()
	res, err := w.p.Provision(context.Background(), req)
	if err != nil {
		t.Fatalf("provision %v: %v", req, err)
	}
	return res
}

func refusalOf(res *pb.ProvisionRepositoryResponse) pb.ProvisionRefusal {
	return res.GetRefused().GetReason()
}

// parentIsOnly says nothing else is in dir than the names given: no staging directory is left.
func (w *provisionWorld) parentIsOnly(t *testing.T, dir string, names ...string) {
	t.Helper()
	got, _ := w.fs.ReadDirNames(dir)
	if strings.Join(got, ",") != strings.Join(names, ",") {
		t.Fatalf("%s holds %v, want %v", dir, got, names)
	}
}

// wantRepository says repo holds the source and a .git with the commit of that tree.
func (w *provisionWorld) wantRepository(t *testing.T, res *pb.ProvisionRepositoryResponse, repo string, source fs.FS) {
	t.Helper()
	got := res.GetProvisioned()
	if got == nil {
		t.Fatalf("got %v, want a provisioned repository", res)
	}
	if got.GetRepository() != repo {
		t.Fatalf("repository %s, want %s", got.GetRepository(), repo)
	}
	want := snapshot(source)
	if have := w.fs.files(repo, false); !maps.Equal(have, want) {
		t.Fatalf("the repository holds %v, want the source %v", have, want)
	}
	if got.GetCommit() != commitOf(want) {
		t.Fatalf("commit %s, want the commit of the source tree", got.GetCommit())
	}
	if _, ok := w.fs.files(repo, true)[".git"]; !ok {
		t.Fatal("the repository has no .git")
	}
}

// Whatever the tree and the name, the repository is the source tree plus a .git with one commit, it
// lands under the default repositories directory with ~ expanded, and nothing else is left beside it.
func TestProvisionedRepositoryIsTheSourceTreePlusGit(t *testing.T) {
	for seed := range uint64(80) {
		r := rand.New(rand.NewPCG(seed, 1))
		w, tree, name := newProvisionWorld(), randomTree(r), randomName(r)
		w.trees.SetVendored("group/example", tree)
		res := w.provision(t, exampleReq("q1", name, "group/example", ""))
		w.wantRepository(t, res, repoPath(reposDir, name), tree)
		w.parentIsOnly(t, reposDir, name)
	}
}

// An empty repository is one commit and no files.
func TestProvisionedEmptyRepositoryHasOneCommitAndNoFiles(t *testing.T) {
	w := newProvisionWorld()
	res := w.provision(t, &pb.ProvisionRepositoryRequest{RequestId: "q", Name: "mine", Source: &pb.ProvisionRepositoryRequest_Empty{Empty: &pb.EmptyRepository{}}})
	w.wantRepository(t, res, repoPath(reposDir, "mine"), fstest.MapFS{})
}

// Break the host at each of its steps in turn, once, and the result is never a repository nor a
// leftover; the same request asked again then makes the repository whole.
func TestAnyOneFailedStepLeavesNothingAndTheRetrySucceeds(t *testing.T) {
	for seed := range uint64(12) {
		r := rand.New(rand.NewPCG(seed, 2))
		tree, name := randomTree(r), randomName(r)
		clean := newProvisionWorld()
		clean.trees.SetVendored("e", tree)
		clean.provision(t, exampleReq("q", name, "e", ""))
		steps := clean.faults.steps
		for k := range steps {
			w := newProvisionWorld()
			w.trees.SetVendored("e", tree)
			w.faults.failAt = k
			res, err := w.p.Provision(context.Background(), exampleReq("q", name, "e", ""))
			if err == nil && res.GetProvisioned() != nil {
				t.Fatalf("seed %d: step %d (%s) failed and a repository was still provisioned", seed, k, w.faults.trace[k])
			}
			w.faults.heal()
			if ok, _ := w.fs.Exists(repoPath(reposDir, name)); ok {
				t.Fatalf("seed %d: step %d (%s): a failed provision left the repository", seed, k, w.faults.trace[k])
			}
			// Steps before the parent exists leave no parent to hold anything.
			if ok, _ := w.fs.Exists(reposDir); ok {
				w.parentIsOnly(t, reposDir)
			}
			res = w.provision(t, exampleReq("q", name, "e", ""))
			w.wantRepository(t, res, repoPath(reposDir, name), tree)
			w.parentIsOnly(t, reposDir, name)
		}
	}
}

// A host that dies at a step leaves what it had made. The next request for the name sweeps that and
// makes the repository whole, with nothing else beside it.
func TestARetryAfterACrashSweepsTheLeftoversAndSucceeds(t *testing.T) {
	for seed := range uint64(12) {
		r := rand.New(rand.NewPCG(seed, 3))
		tree, name := randomTree(r), randomName(r)
		clean := newProvisionWorld()
		clean.trees.SetVendored("e", tree)
		clean.provision(t, exampleReq("q", name, "e", ""))
		for k := range clean.faults.steps {
			w := newProvisionWorld()
			w.trees.SetVendored("e", tree)
			w.faults.failAt, w.faults.crash = k, true
			_, _ = w.p.Provision(context.Background(), exampleReq("q", name, "e", ""))
			w.faults.heal()
			if ok, _ := w.fs.Exists(repoPath(reposDir, name)); ok && k < clean.faults.steps-1 {
				// Only the last step, the rename, can have made it: a crash before it leaves no repository.
				t.Fatalf("seed %d: a crash at step %d (%s) left the repository", seed, k, w.faults.trace[k])
			}
			res := w.provision(t, exampleReq("q", name, "e", ""))
			w.wantRepository(t, res, repoPath(reposDir, name), tree)
			w.parentIsOnly(t, reposDir, name)
		}
	}
}

// Two requests for one name with different ids: exactly one makes it, the other is refused as taken
// and told where it is. Requests with the winner's id all get the one success.
func TestConcurrentRequestsForOneNameHaveExactlyOneWinner(t *testing.T) {
	for seed := range uint64(30) {
		r := rand.New(rand.NewPCG(seed, 4))
		w, tree, name := newProvisionWorld(), randomTree(r), randomName(r)
		w.trees.SetVendored("e", tree)
		n := 2 + r.IntN(6)
		results := make([]*pb.ProvisionRepositoryResponse, n)
		var wg sync.WaitGroup
		start := make(chan struct{})
		for i := range n {
			wg.Add(1)
			go func() {
				defer wg.Done()
				<-start
				res, err := w.p.Provision(context.Background(), exampleReq(fmt.Sprintf("q%d", i), name, "e", ""))
				if err != nil {
					t.Error(err)
				}
				results[i] = res
			}()
		}
		close(start)
		wg.Wait()
		winners := 0
		for _, res := range results {
			switch {
			case res.GetProvisioned() != nil:
				winners++
			case refusalOf(res) != pb.ProvisionRefusal_PROVISION_REFUSAL_NAME_TAKEN || res.GetRefused().GetExistingRepository() != repoPath(reposDir, name):
				t.Fatalf("seed %d: loser got %v, want NAME_TAKEN naming the repository", seed, res)
			}
		}
		if winners != 1 {
			t.Fatalf("seed %d: %d winners of %d requests, want exactly one", seed, winners, n)
		}
		w.parentIsOnly(t, reposDir, name)
		if have := w.fs.files(repoPath(reposDir, name), false); !maps.Equal(have, snapshot(tree)) {
			t.Fatalf("seed %d: the winner's repository is not the source", seed)
		}

		// The same request again, even at once, is the same success.
		a, b := w.provision(t, exampleReq("q0", name, "e", "")), w.provision(t, exampleReq("q0", name, "e", ""))
		if results[0].GetProvisioned() != nil && (a.GetProvisioned() == nil || a.GetProvisioned().GetCommit() != b.GetProvisioned().GetCommit()) {
			t.Fatalf("seed %d: the winner's retry got %v and %v", seed, a, b)
		}
	}
}

// Another process makes the name between the check and the rename: this request is refused as taken
// and its staging is gone.
func TestLosingTheRenameToAnotherProcessIsNameTaken(t *testing.T) {
	w := newProvisionWorld()
	w.trees.SetVendored("e", fstest.MapFS{"a": {Data: []byte("x")}})
	w.fs.beforeRename = func(m *memFS) {
		m.mu.Lock()
		defer m.mu.Unlock()
		_ = m.put(repoPath(reposDir, "mine"), memNode{dir: true})
		_ = m.put(repoPath(reposDir, "mine")+"/theirs", memNode{data: []byte("theirs")})
	}
	res := w.provision(t, exampleReq("q", "mine", "e", ""))
	if refusalOf(res) != pb.ProvisionRefusal_PROVISION_REFUSAL_NAME_TAKEN {
		t.Fatalf("got %v, want NAME_TAKEN", res)
	}
	w.parentIsOnly(t, reposDir, "mine")
	if got := w.fs.files(repoPath(reposDir, "mine"), true); len(got) != 1 {
		t.Fatalf("the other process's repository was changed: %v", got)
	}
}

// A name that is taken is refused, never overwritten, and says which repository holds it.
func TestATakenNameIsRefusedAndTheRepositoryIsKept(t *testing.T) {
	w := newProvisionWorld()
	w.trees.SetVendored("e", fstest.MapFS{"a": {Data: []byte("x")}})
	w.provision(t, exampleReq("q1", "mine", "e", ""))
	before := w.fs.files(repoPath(reposDir, "mine"), true)
	res := w.provision(t, exampleReq("q2", "mine", "e", ""))
	if refusalOf(res) != pb.ProvisionRefusal_PROVISION_REFUSAL_NAME_TAKEN || res.GetRefused().GetExistingRepository() != repoPath(reposDir, "mine") {
		t.Fatalf("got %v, want NAME_TAKEN with the existing repository", res)
	}
	if !maps.Equal(before, w.fs.files(repoPath(reposDir, "mine"), true)) {
		t.Fatal("a refused request changed the repository")
	}
	// The same id with another source is another request.
	w.trees.SetVendored("f", fstest.MapFS{})
	if res := w.provision(t, exampleReq("q1", "mine", "f", "")); refusalOf(res) != pb.ProvisionRefusal_PROVISION_REFUSAL_NAME_TAKEN {
		t.Fatalf("the same id for another example: %v, want NAME_TAKEN", res)
	}
}

// Names outside lowercase letters, digits and dashes are refused before anything is touched, and no
// name reaches outside the parent.
func TestInvalidNamesAreRefusedAndTouchNothing(t *testing.T) {
	alphabet := []rune("abcxyz019-_./\\~ ABC\x00é")
	r := rand.New(rand.NewPCG(5, 5))
	w := newProvisionWorld()
	w.trees.SetVendored("e", fstest.MapFS{})
	names := []string{"", "..", ".", "a/b", "../x", "../../etc", "-a", "A", "a.b", "a_b", "/abs", strings.Repeat("a", 64)}
	for range 300 {
		n := make([]rune, 1+r.IntN(12))
		for i := range n {
			n[i] = alphabet[r.IntN(len(alphabet))]
		}
		names = append(names, string(n))
	}
	for _, name := range names {
		if daemon.ValidRepositoryName(name) {
			continue
		}
		res := w.provision(t, exampleReq("q", name, "e", ""))
		if refusalOf(res) != pb.ProvisionRefusal_PROVISION_REFUSAL_NAME_INVALID {
			t.Fatalf("name %q: %v, want NAME_INVALID", name, res)
		}
	}
	if w.faults.steps != 0 {
		t.Fatalf("a refused name took %d steps on the host, want none", w.faults.steps)
	}
}

// A parent is absolute with no .., or a ~ or ~/ that the host expands; anything else is refused.
func TestParentTraversalAndRelativeParentsAreRefused(t *testing.T) {
	w := newProvisionWorld()
	w.trees.SetVendored("e", fstest.MapFS{})
	for _, parent := range []string{"relative/dir", "/a/../b", "/..", "../x", "~other/repos", "~/../etc"} {
		res := w.provision(t, exampleReq("q", "mine", "e", parent))
		if refusalOf(res) != pb.ProvisionRefusal_PROVISION_REFUSAL_PARENT_NOT_WRITABLE {
			t.Fatalf("parent %q: %v, want PARENT_NOT_WRITABLE", parent, res)
		}
	}
	if w.faults.steps != 0 {
		t.Fatalf("a refused parent took %d steps on the host", w.faults.steps)
	}
	for parent, want := range map[string]string{"~": homeDir, "~/work": homeDir + "/work", "/srv/repos": "/srv/repos"} {
		res := w.provision(t, exampleReq("q", "mine", "e", parent))
		if res.GetProvisioned().GetRepository() != want+"/mine" {
			t.Fatalf("parent %q: %v, want the repository in %s", parent, res, want)
		}
	}
}

// A parent that is a file, or that cannot be made, is refused with nothing made.
func TestAParentThatCannotBeMadeIsRefused(t *testing.T) {
	w := newProvisionWorld()
	w.trees.SetVendored("e", fstest.MapFS{})
	_ = w.fs.MkdirAll("/data")
	_ = w.fs.WriteFile("/data/file", []byte("x"), 0o644)
	res := w.provision(t, exampleReq("q", "mine", "e", "/data/file/sub"))
	if refusalOf(res) != pb.ProvisionRefusal_PROVISION_REFUSAL_PARENT_NOT_WRITABLE {
		t.Fatalf("got %v, want PARENT_NOT_WRITABLE", res)
	}
}

// An example the release lacks, a source that cannot be copied, and one pinned to nothing are each
// refused with their own reason, and name the example.
func TestExampleRefusals(t *testing.T) {
	w := newProvisionWorld()
	if res := w.provision(t, exampleReq("q", "mine", "no/such", "")); refusalOf(res) != pb.ProvisionRefusal_PROVISION_REFUSAL_EXAMPLE_NOT_FOUND || !strings.Contains(res.GetRefused().GetDetail(), "no/such") {
		t.Fatalf("an unknown example: %v", res)
	}
	w.trees.Set("ext/unpinned", daemon.ExampleSource{Clone: &daemon.CloneSpec{URL: "https://example.invalid/r.git"}})
	if res := w.provision(t, exampleReq("q", "mine", "ext/unpinned", "")); refusalOf(res) != pb.ProvisionRefusal_PROVISION_REFUSAL_COPY_FAILED || !strings.Contains(res.GetRefused().GetDetail(), "ext/unpinned") {
		t.Fatalf("an unpinned external example: %v, want COPY_FAILED naming it", res)
	}
	w.trees.Fail("ext/gone", &daemon.ExampleUnavailable{Reason: daemon.ExampleUnpinned, Detail: "the registry has no pin for ext/gone"})
	if res := w.provision(t, exampleReq("q", "mine", "ext/gone", "")); refusalOf(res) != pb.ProvisionRefusal_PROVISION_REFUSAL_COPY_FAILED || !strings.Contains(res.GetRefused().GetDetail(), "ext/gone") {
		t.Fatalf("an unavailable example: %v", res)
	}
	for name, bad := range map[string]fstest.MapFS{
		"links": {"a": {Data: []byte("x")}, "l": {Mode: fs.ModeSymlink, Data: []byte("a")}},
		"git":   {"sub/.git/config": {Data: []byte("x")}},
	} {
		w.trees.SetVendored(name, bad)
		if res := w.provision(t, exampleReq("q", "mine", name, "")); refusalOf(res) != pb.ProvisionRefusal_PROVISION_REFUSAL_COPY_FAILED {
			t.Fatalf("source %s: %v, want COPY_FAILED", name, res)
		}
	}
	w.parentIsOnly(t, reposDir)
}

// An external example is its pinned revision with the vendored files over it, and none of the clone's
// history. A clone that cannot be made is NEEDS_NETWORK; a revision the remote lacks is COPY_FAILED.
func TestExternalExampleIsThePinnedCloneWithVendoredPathsOverIt(t *testing.T) {
	w := newProvisionWorld()
	const url = "https://example.invalid/r.git"
	w.git.remotes[url] = map[string]map[string]string{
		"pin1": {"src/lib.py": "pinned", "README": "remote readme", "task/OBJECTIVE.md": "remote objective"},
		"pin2": {"src/lib.py": "newer"},
	}
	vendored := fstest.MapFS{"task/OBJECTIVE.md": {Data: []byte("vendored objective"), Mode: 0o644}, "extra": {Data: []byte("x"), Mode: 0o644}}
	w.trees.Set("ext/e", daemon.ExampleSource{Vendored: vendored, Clone: &daemon.CloneSpec{URL: url, Revision: "pin1"}})
	res := w.provision(t, exampleReq("q", "mine", "ext/e", ""))
	want := snapshot(fstest.MapFS{
		"src":               {Mode: fs.ModeDir | 0o755},
		"task":              {Mode: fs.ModeDir | 0o755},
		"src/lib.py":        {Data: []byte("pinned"), Mode: 0o644},
		"README":            {Data: []byte("remote readme"), Mode: 0o644},
		"task/OBJECTIVE.md": {Data: []byte("vendored objective"), Mode: 0o644},
		"extra":             {Data: []byte("x"), Mode: 0o644},
	})
	repo := repoPath(reposDir, "mine")
	if res.GetProvisioned() == nil || !maps.Equal(w.fs.files(repo, false), want) {
		t.Fatalf("got %v with %v, want the pin with vendored paths over it %v", res, w.fs.files(repo, false), want)
	}
	if got := w.fs.files(repo, true)[".git/COMMIT"]; !strings.HasSuffix(got, "") || w.fs.files(repo, true)[".git"] != "dir" {
		t.Fatalf("the repository's git directory is not a fresh one: %v", got)
	}

	w.trees.Set("ext/offline", daemon.ExampleSource{Clone: &daemon.CloneSpec{URL: "https://example.invalid/none.git", Revision: "pin1"}})
	if res := w.provision(t, exampleReq("q", "off", "ext/offline", "")); refusalOf(res) != pb.ProvisionRefusal_PROVISION_REFUSAL_NEEDS_NETWORK {
		t.Fatalf("an unreachable remote: %v, want NEEDS_NETWORK", res)
	}
	w.trees.Set("ext/missing", daemon.ExampleSource{Clone: &daemon.CloneSpec{URL: url, Revision: "nope"}})
	if res := w.provision(t, exampleReq("q", "miss", "ext/missing", "")); refusalOf(res) != pb.ProvisionRefusal_PROVISION_REFUSAL_COPY_FAILED || !strings.Contains(res.GetRefused().GetDetail(), "ext/missing") {
		t.Fatalf("a revision the remote lacks: %v, want COPY_FAILED naming the example", res)
	}
	w.parentIsOnly(t, reposDir, "mine")
}

// A cancelled call is its error, not a refusal, and leaves nothing.
func TestACancelledProvisionIsAnErrorAndLeavesNothing(t *testing.T) {
	w := newProvisionWorld()
	w.trees.SetVendored("e", fstest.MapFS{"a": {Data: []byte("x")}})
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	w.git.fs.faults.failAt = 6
	if res, err := w.p.Provision(ctx, exampleReq("q", "mine", "e", "")); err == nil && res.GetProvisioned() != nil {
		t.Fatalf("a cancelled provision made %v", res)
	}
	w.faults.heal()
	w.parentIsOnly(t, reposDir)
}

// The real file system and git: the repository is the source with its modes, one commit, a clean tree,
// and the same request again is the same success. A clone is at its pin, without its history.
func TestProvisionWithRealGitAndFiles(t *testing.T) {
	root := t.TempDir()
	source := fstest.MapFS{
		"OBJECTIVE.md":          {Data: []byte("objective\n"), Mode: 0o644},
		"bin/run.sh":            {Data: []byte("#!/bin/sh\n"), Mode: 0o755},
		"empty":                 {Mode: fs.ModeDir | 0o755},
		".vibesys/tasks/t.toml": {Data: []byte("name='t'\n"), Mode: 0o644},
	}
	var trees daemontest.FakeExampleTrees
	trees.SetVendored("e", source)
	p := &Provisioner{Trees: &trees, FS: OSProvisionFS{}, Git: ExecGit{Bounds: GitTimeouts{}}, Default: filepath.Join(root, "repos"), Home: root}
	req := exampleReq("q", "mine", "e", "")
	res, err := p.Provision(context.Background(), req)
	if err != nil || res.GetProvisioned() == nil {
		t.Fatalf("got %v, %v", res, err)
	}
	repo := res.GetProvisioned().GetRepository()
	run := func(args ...string) string {
		out, err := exec.Command("git", append([]string{"-C", repo}, args...)...).CombinedOutput()
		if err != nil {
			t.Fatalf("git %v: %v: %s", args, err, out)
		}
		return strings.TrimSpace(string(out))
	}
	if got := run("rev-parse", "HEAD"); got != res.GetProvisioned().GetCommit() {
		t.Fatalf("HEAD %s, response %s", got, res.GetProvisioned().GetCommit())
	}
	if got := run("rev-list", "--count", "HEAD"); got != "1" {
		t.Fatalf("%s commits, want 1", got)
	}
	if got := run("status", "--porcelain"); got != "" {
		t.Fatalf("the work tree is not clean: %s", got)
	}
	if got := run("ls-files", "-s", "bin/run.sh"); !strings.HasPrefix(got, "100755") {
		t.Fatalf("run.sh is %s, want the executable bit kept", got)
	}
	if got := run("ls-files"); got != ".vibesys/tasks/t.toml\nOBJECTIVE.md\nbin/run.sh" {
		t.Fatalf("committed files %q", got)
	}
	if again, err := p.Provision(context.Background(), req); err != nil || again.GetProvisioned().GetCommit() != res.GetProvisioned().GetCommit() {
		t.Fatalf("the same request again: %v, %v", again, err)
	}
	if other, _ := p.Provision(context.Background(), exampleReq("other", "mine", "e", "")); refusalOf(other) != pb.ProvisionRefusal_PROVISION_REFUSAL_NAME_TAKEN {
		t.Fatalf("another request for the name: %v", other)
	}

	remote := filepath.Join(root, "remote")
	sh := func(dir string, args ...string) string {
		out, err := exec.Command("git", append([]string{"-C", dir, "-c", "user.name=t", "-c", "user.email=t@t"}, args...)...).CombinedOutput()
		if err != nil {
			t.Fatalf("git %v: %v: %s", args, err, out)
		}
		return strings.TrimSpace(string(out))
	}
	if out, err := exec.Command("git", "init", "-q", remote).CombinedOutput(); err != nil {
		t.Fatalf("%v: %s", err, out)
	}
	write(t, filepath.Join(remote, "lib.txt"), "one")
	write(t, filepath.Join(remote, "task.txt"), "remote")
	sh(remote, "add", "-A")
	sh(remote, "commit", "-q", "-m", "one")
	pin := sh(remote, "rev-parse", "HEAD")
	write(t, filepath.Join(remote, "lib.txt"), "two")
	sh(remote, "commit", "-qam", "two")
	trees.Set("ext", daemon.ExampleSource{Vendored: fstest.MapFS{"task.txt": {Data: []byte("vendored"), Mode: 0o644}}, Clone: &daemon.CloneSpec{URL: remote, Revision: pin}})
	res, err = p.Provision(context.Background(), exampleReq("q", "cloned", "ext", ""))
	if err != nil || res.GetProvisioned() == nil {
		t.Fatalf("clone: %v, %v", res, err)
	}
	cloned := res.GetProvisioned().GetRepository()
	if got := readTestFile(t, filepath.Join(cloned, "lib.txt")); got != "one" {
		t.Fatalf("lib.txt is %q, want the pinned one", got)
	}
	if got := readTestFile(t, filepath.Join(cloned, "task.txt")); got != "vendored" {
		t.Fatalf("task.txt is %q, want the vendored file", got)
	}
	if got := sh(cloned, "rev-list", "--count", "HEAD"); got != "1" {
		t.Fatalf("%s commits in the clone, want only the initial one", got)
	}
	trees.Set("ext2", daemon.ExampleSource{Clone: &daemon.CloneSpec{URL: filepath.Join(root, "none"), Revision: pin}})
	if res, _ := p.Provision(context.Background(), exampleReq("q", "off", "ext2", "")); refusalOf(res) != pb.ProvisionRefusal_PROVISION_REFUSAL_NEEDS_NETWORK {
		t.Fatalf("an unreachable remote: %v", res)
	}
	if got, _ := (OSProvisionFS{}).ReadDirNames(filepath.Join(root, "repos")); len(got) != 2 {
		t.Fatalf("the repositories directory holds %v, want mine and cloned", got)
	}
}
