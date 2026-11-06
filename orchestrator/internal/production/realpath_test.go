package production

import (
	"fmt"
	"math/rand/v2"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// realChecks is StartChecks over the real file system.
var realChecks = StartChecks{Sidecars: readyLauncher{}, Tree: OSTree{}}

// newRepository is a git work tree, as far as a start checks it, holding a task folder.
func newRepository(t *testing.T) string {
	t.Helper()
	dir, err := filepath.EvalSymlinks(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	for _, sub := range []string{".git", filepath.Join(".vibesys", "tasks", "demo")} {
		if err := os.MkdirAll(filepath.Join(dir, sub), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

// Any name for a repository, through any chain of links and with parts of the path that are links,
// resolves to the one real directory: that is what keeps one repository to one run.
func TestEveryNameOfARepositoryResolvesToTheRealDirectory(t *testing.T) {
	real := newRepository(t)
	for seed := uint64(1); seed <= 40; seed++ {
		rng := rand.New(rand.NewPCG(seed, 11))
		base, err := filepath.EvalSymlinks(t.TempDir())
		if err != nil {
			t.Fatal(err)
		}
		current := real
		for i := range 1 + rng.IntN(4) {
			link := filepath.Join(base, fmt.Sprintf("link-%d", i))
			if err := os.Symlink(current, link); err != nil {
				t.Fatal(err)
			}
			current = link
			if rng.IntN(2) == 0 { // A link to the parent folder, with the repository named through it.
				parent := filepath.Join(base, fmt.Sprintf("parent-%d", i))
				if err := os.Symlink(filepath.Dir(current), parent); err != nil {
					t.Fatal(err)
				}
				current = filepath.Join(parent, filepath.Base(current))
			}
			got, err := realChecks.CheckStart(current, "demo")
			if err != nil || got != real {
				t.Fatalf("seed %d: %s resolved to %q, %v, want %s", seed, current, got, err, real)
			}
		}
	}
}

// A repository path that does not exist is refused naming it as given.
func TestAMissingRepositoryIsNamedAsGiven(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "gone")
	_, err := realChecks.CheckStart(missing, "demo")
	if r := refusal(t, err); !strings.Contains(r.Reason, missing) {
		t.Fatalf("reason %q lacks %s", r.Reason, missing)
	}
}
