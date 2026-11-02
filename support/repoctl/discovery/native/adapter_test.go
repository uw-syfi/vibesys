package native

import (
	"fmt"
	"strings"
	"testing"

	"repoctl/discovery"
)

// fakeGit answers the adapter's `git ls-files` with a fixed manifest list, so
// the tests drive Discover through its public interface without a repository.
func fakeGit(manifestPaths ...string) discovery.RunCommand {
	return func(_, _ string, _ ...string) ([]byte, error) {
		return []byte(strings.Join(manifestPaths, "\x00") + "\x00"), nil
	}
}

func dockerSpec(selected []string, excluded map[string]string) discovery.Spec {
	return discovery.Spec{
		Adapter: "manifest_directories", Class: "dockerfile", IDPrefix: "dockerfile:",
		OwnershipGroup: "dockerfiles", Manifests: map[string]string{"Dockerfile": "docker"},
		ScopeRoots: []string{"examples"}, SelectedRoots: selected, ExcludedRoots: excluded,
		SelectedJobs: []string{"dockerfiles"},
	}
}

var contexts = []string{"examples/a", "examples/b", "examples/c/task", "examples/d"}

func dockerfiles() discovery.RunCommand {
	paths := make([]string, len(contexts))
	for i, dir := range contexts {
		paths[i] = dir + "/Dockerfile"
	}
	return fakeGit(paths...)
}

// Every way to split the contexts into built and excluded is accepted, and each
// component carries exactly the job (or none) its listing implies.
func TestEveryPartitionIntoBuiltAndExcludedIsDiscoveredWithMatchingJobs(t *testing.T) {
	for mask := 0; mask < 1<<len(contexts); mask++ {
		var built []string
		excluded := map[string]string{}
		for i, dir := range contexts {
			if mask&(1<<i) != 0 {
				excluded[dir] = "needs a GPU"
			} else {
				built = append(built, dir)
			}
		}
		components, err := Adapter{Run: dockerfiles()}.Discover(".", dockerSpec(built, excluded))
		if err != nil {
			t.Fatalf("mask %b: %v", mask, err)
		}
		if len(components) != len(contexts) {
			t.Fatalf("mask %b: %d components, want %d", mask, len(components), len(contexts))
		}
		for _, c := range components {
			_, isExcluded := excluded[c.Roots[0]]
			if c.Selected == isExcluded {
				t.Errorf("mask %b: %s selected=%v but excluded=%v", mask, c.ID, c.Selected, isExcluded)
			}
			if wantJobs := map[bool]int{true: 0, false: 1}[isExcluded]; len(c.Jobs) != wantJobs {
				t.Errorf("mask %b: %s jobs=%v", mask, c.ID, c.Jobs)
			}
			if c.Language != "docker" || c.Class != "dockerfile" {
				t.Errorf("mask %b: %s language=%q class=%q", mask, c.ID, c.Language, c.Class)
			}
		}
	}
}

// A Dockerfile that is neither built nor excluded fails discovery, naming it,
// whichever one is left out.
func TestADockerfileNeitherBuiltNorExcludedIsRejectedByName(t *testing.T) {
	for left := range contexts {
		var built []string
		for i, dir := range contexts {
			if i != left {
				built = append(built, dir)
			}
		}
		_, err := Adapter{Run: dockerfiles()}.Discover(".", dockerSpec(built, nil))
		if err == nil || !strings.Contains(err.Error(), contexts[left]) {
			t.Errorf("leaving out %s: error = %v", contexts[left], err)
		}
	}
}

func TestExcludedRootMisconfigurationsAreRejected(t *testing.T) {
	all := append([]string(nil), contexts...)
	cases := []struct {
		name     string
		selected []string
		excluded map[string]string
		want     string
	}{
		{"blank reason", all[1:], map[string]string{contexts[0]: "  "}, "needs a reason"},
		{"both built and excluded", all, map[string]string{contexts[0]: "why"}, "both selected and excluded"},
		{"no Dockerfile there", all, map[string]string{"examples/missing": "why"}, "excluded root has no configured manifest"},
		{"unsafe path", all, map[string]string{"../escape": "why"}, "unsafe"},
	}
	for _, tc := range cases {
		_, err := Adapter{Run: dockerfiles()}.Discover(".", dockerSpec(tc.selected, tc.excluded))
		if err == nil || !strings.Contains(err.Error(), tc.want) {
			t.Errorf("%s: error = %v, want it to contain %q", tc.name, err, tc.want)
		}
	}
}

func TestADockerfileOutsideScopeNeedsNoListing(t *testing.T) {
	run := fakeGit("tests/image/Dockerfile", "examples/a/Dockerfile")
	spec := dockerSpec([]string{"examples/a"}, nil)
	components, err := Adapter{Run: run}.Discover(".", spec)
	if err != nil {
		t.Fatal(err)
	}
	if got := fmt.Sprint(len(components)); got != "2" {
		t.Fatalf("components = %s", got)
	}
}
