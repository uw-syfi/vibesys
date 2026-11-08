package production

import (
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// watchedTree records every directory read through it, so a test can say which folders a listing
// looked into.
type watchedTree struct {
	fakeTree
	reads []string
}

func (w *watchedTree) ReadDirNames(path string) ([]string, error) {
	w.reads = append(w.reads, path)
	return w.fakeTree.ReadDirNames(path)
}

// For any folder of any tree, the listing is the folder's own children, hidden ones only on request,
// sorted, cut at the cap with Truncated set exactly when something was cut; each entry's marks are
// what its own folder holds; and no folder below the listed one is read except a child's task folder.
func TestAListingIsTheFoldersOwnChildrenFilteredAndCapped(t *testing.T) {
	for seed := range uint64(200) {
		rng := rand.New(rand.NewPCG(seed, 7))
		tree := fakeTree{"/root": true}
		visible := map[string]bool{}
		n := rng.IntN(MaxDirectoryEntries * 2)
		if rng.IntN(4) > 0 {
			n = rng.IntN(20)
		}
		for i := range n {
			name := fmt.Sprintf("e%03d", i)
			if rng.IntN(5) == 0 {
				name = "." + name
			}
			child := "/root/" + name
			isDir := rng.IntN(3) > 0
			tree[child] = isDir
			if isDir {
				if rng.IntN(2) == 0 {
					tree[child+"/.git"] = true
				}
				if rng.IntN(2) == 0 {
					tree[child+"/.vibesys/tasks/t"] = true
				}
				tree[child+"/deep/deeper/file"] = false // below the listed folder: never read
			}
			visible[name] = isDir
		}
		for _, hidden := range []bool{false, true} {
			watched := &watchedTree{fakeTree: tree}
			got, err := DirectoryLister{Tree: watched}.List(context.Background(), "/root/", hidden)
			if err != nil {
				t.Fatalf("seed %d: %v", seed, err)
			}
			var names []string
			for name := range visible {
				if hidden || !strings.HasPrefix(name, ".") {
					names = append(names, name)
				}
			}
			slices.Sort(names)
			truncated := len(names) > MaxDirectoryEntries
			if truncated {
				names = names[:MaxDirectoryEntries]
			}
			var gotNames []string
			for _, e := range got.Entries {
				gotNames = append(gotNames, e.Name)
				child := "/root/" + e.Name
				if e.IsDirectory != tree[child] ||
					e.IsGitRepository != (e.IsDirectory && tree[child+"/.git"]) ||
					e.HasTasks != (e.IsDirectory && tree[child+"/.vibesys/tasks/t"]) {
					t.Fatalf("seed %d: entry %+v does not match what %s holds", seed, e, child)
				}
			}
			if !slices.Equal(gotNames, names) || got.Truncated != truncated {
				t.Fatalf("seed %d hidden=%v: got %d entries truncated=%v, want %d truncated=%v", seed, hidden, len(gotNames), got.Truncated, len(names), truncated)
			}
			for _, read := range watched.reads {
				if read != "/root" && !strings.HasSuffix(read, "/.vibesys/tasks") {
					t.Fatalf("seed %d: read %s, which is not the folder or a child's tasks", seed, read)
				}
			}
		}
	}
}

// A time bound that has ended gives what was read so far, marked truncated, and never fails.
func TestAListingWhoseBoundEndedIsTruncated(t *testing.T) {
	tree := fakeTree{"/root": true, "/root/a": true, "/root/b": true}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	got, err := DirectoryLister{Tree: tree}.List(ctx, "/root", false)
	if err != nil || !got.Truncated || len(got.Entries) != 0 {
		t.Fatalf("got %+v, %v, want an empty truncated listing", got, err)
	}
}

// A path that cannot be listed is refused with the fault and words that name it; ~ is the host's home.
func TestAListingRefusesWhatItCannotListAndResolvesHome(t *testing.T) {
	tree := fakeTree{"/home/u/code": true, "/home/u/code/app": true, "/file": false, "/etc": true}
	lister := DirectoryLister{Tree: tree}
	cases := []struct {
		path  string
		fault daemon.DirectoryFault
	}{
		{"relative/dir", daemon.DirectoryNotAbsolute},
		{"~other", daemon.DirectoryNotAbsolute},
		{"/missing", daemon.DirectoryNotFound},
		{"/file", daemon.DirectoryNotADirectory},
	}
	for _, c := range cases {
		_, err := lister.List(context.Background(), c.path, false)
		var fault *daemon.DirectoryError
		if !errors.As(err, &fault) || fault.Fault != c.fault || !strings.Contains(fault.Reason, strings.TrimSuffix(c.path, "/")) {
			t.Errorf("%q gave %v, want fault %d naming the path", c.path, err, c.fault)
		}
	}
	for _, path := range []string{"~/code", "~/code/", "/home/u/code"} {
		got, err := lister.List(context.Background(), path, false)
		if err != nil || len(got.Entries) != 1 || got.Entries[0].Name != "app" {
			t.Errorf("%q gave %+v, %v, want the one folder app", path, got, err)
		}
	}
}

// On a real file system: marks, hidden entries, a link to a folder, a broken link, a file, and a
// folder the daemon may not read.
func TestAListingOfARealFolder(t *testing.T) {
	dir := newRepository(t) // a work tree holding .vibesys/tasks/demo
	write(t, filepath.Join(dir, "sub", "README"), "x")
	write(t, filepath.Join(dir, ".hidden", "f"), "x")
	write(t, filepath.Join(dir, "plain.txt"), "x")
	if err := os.Symlink(filepath.Join(dir, "sub"), filepath.Join(dir, "link")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(dir, "absent"), filepath.Join(dir, "broken")); err != nil {
		t.Fatal(err)
	}
	parent := filepath.Dir(dir)
	got, err := DirectoryLister{Tree: OSTree{}}.List(context.Background(), parent, false)
	if err != nil {
		t.Fatal(err)
	}
	want := daemon.DirectoryEntry{Name: filepath.Base(dir), IsDirectory: true, IsGitRepository: true, HasTasks: true}
	if len(got.Entries) != 1 || got.Entries[0] != want {
		t.Fatalf("parent lists %+v, want %+v", got.Entries, want)
	}
	inside, err := DirectoryLister{Tree: OSTree{}}.List(context.Background(), dir+"/", false)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, e := range inside.Entries {
		names = append(names, e.Name)
		if e.Name == "link" && !e.IsDirectory {
			t.Errorf("a link to a folder is a directory, got %+v", e)
		}
	}
	if !slices.Equal(names, []string{"link", "plain.txt", "sub"}) { // .git, .vibesys and .hidden are hidden; broken is dropped
		t.Fatalf("lists %v", names)
	}
	withHidden, err := DirectoryLister{Tree: OSTree{}}.List(context.Background(), dir, true)
	var hiddenNames []string
	for _, e := range withHidden.Entries {
		hiddenNames = append(hiddenNames, e.Name)
	}
	if err != nil || !slices.Equal(hiddenNames, []string{".git", ".hidden", ".vibesys", "link", "plain.txt", "sub"}) {
		t.Fatalf("hidden listing %v, %v, want the dot entries first", hiddenNames, err)
	}
	if _, err := (DirectoryLister{Tree: OSTree{}}).List(context.Background(), filepath.Join(dir, "plain.txt"), false); err == nil {
		t.Fatal("listed a file")
	}
	locked := filepath.Join(dir, "locked")
	if err := os.Mkdir(locked, 0o000); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(locked, 0o755) })
	if _, err := os.ReadDir(locked); err == nil {
		return // The tests run as a user who may read anything: there is no permission to be denied.
	}
	_, err = DirectoryLister{Tree: OSTree{}}.List(context.Background(), locked, false)
	var fault *daemon.DirectoryError
	if !errors.As(err, &fault) || fault.Fault != daemon.DirectoryDenied || !strings.Contains(fault.Reason, locked) {
		t.Fatalf("a locked folder gave %v, want DirectoryDenied naming it", err)
	}
}

// Tasks are the folders under .vibesys/tasks, sorted, and none for a repository without that folder.
func TestTasksAreTheFoldersUnderTheTasksFolder(t *testing.T) {
	dir := newRepository(t)
	write(t, filepath.Join(dir, ".vibesys", "tasks", "alpha", "OBJECTIVE.md"), "x")
	write(t, filepath.Join(dir, ".vibesys", "tasks", "stray.txt"), "x")
	checks := StartChecks{Sidecars: readyLauncher{}, Parts: readyParts{}, Tree: OSTree{}}
	got, err := checks.Tasks(dir)
	if err != nil || !slices.Equal(got, []string{"alpha", "demo"}) {
		t.Fatalf("got %v, %v, want alpha and demo", got, err)
	}
	if got, err := checks.Tasks(t.TempDir()); err != nil || len(got) != 0 {
		t.Fatalf("a repository with no tasks folder gave %v, %v, want none", got, err)
	}
}
