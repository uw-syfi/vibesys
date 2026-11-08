package release_test

import (
	"errors"
	"fmt"
	"io/fs"
	"math/rand/v2"
	"slices"
	"testing"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/release"
	"github.com/uw-syfi/vibesys/orchestrator/release/releasetest"
)

const pinned = "0123456789abcdef0123456789abcdef01234567"

func twoExamples() []releasetest.Example {
	return []releasetest.Example{
		{ID: "data-structures/stack", Task: "default", Files: map[string]string{".vibesys/tasks/default/vibesys.input.toml": "a", "src/main.rs": "b"}},
		{ID: "model-serving/repositories/vllm", Task: "default",
			Files: map[string]string{".vibesys/tasks/default/vibesys.input.toml": "c"},
			Clone: &release.CloneSpec{URL: "https://example.invalid/vllm.git", Revision: pinned}},
	}
}

func TestAnExampleIsServedAsItsFilesAndItsPinnedClone(t *testing.T) {
	r := open(t, releasetest.Tree(t, dir, releasetest.WithExamples(twoExamples()...)))
	if err := r.Verify(release.Archived); err != nil {
		t.Fatal(err)
	}

	own, err := r.ExampleSource("data-structures/stack")
	if err != nil {
		t.Fatal(err)
	}
	if own.Clone != nil {
		t.Fatalf("an example that is all vendored has a clone: %+v", own.Clone)
	}
	if got, err := fs.ReadFile(own.Vendored, "src/main.rs"); err != nil || string(got) != "b" {
		t.Fatalf("src/main.rs = %q, %v", got, err)
	}

	cloned, err := r.ExampleSource("model-serving/repositories/vllm")
	if err != nil {
		t.Fatal(err)
	}
	if cloned.Clone == nil || *cloned.Clone != (release.CloneSpec{URL: "https://example.invalid/vllm.git", Revision: pinned}) {
		t.Fatalf("clone = %+v", cloned.Clone)
	}
	if _, err := fs.ReadFile(cloned.Vendored, ".vibesys/tasks/default/vibesys.input.toml"); err != nil {
		t.Fatal(err)
	}
}

// Only ids the catalogue lists have a source: whatever else is asked, including names that climb
// out of the examples folder, is not found, and never reads a file.
func TestOnlyListedExamplesHaveASource(t *testing.T) {
	r := open(t, releasetest.Tree(t, dir, releasetest.WithExamples(twoExamples()...)))
	for _, id := range []string{"", "..", "../bin/vibesys-host", "data-structures", "data-structures/stack/src", "/data-structures/stack", "data-structures/../data-structures/stack", "release.json", "nope"} {
		_, err := r.ExampleSource(id)
		var gone *release.ExampleNotFoundError
		if !errors.As(err, &gone) || gone.Example != id {
			t.Errorf("%q: err = %v, want ExampleNotFoundError", id, err)
		}
	}
}

// Whatever subset of the example folders is gone, verify names exactly those folders.
func TestVerifyNamesTheExampleFoldersThatAreGone(t *testing.T) {
	rng := rand.New(rand.NewPCG(7, 11))
	for range 60 {
		var examples []releasetest.Example
		for i := range 1 + rng.IntN(6) {
			examples = append(examples, releasetest.Example{ID: fmt.Sprintf("family/ex%d", i), Task: "default", Files: map[string]string{"f": "x"}})
		}
		fsys := releasetest.Tree(t, dir, releasetest.WithExamples(examples...))
		var gone []string
		for _, e := range examples {
			if rng.IntN(2) == 0 {
				continue
			}
			folder := "opt/vs/examples/" + e.ID
			delete(fsys, folder)
			delete(fsys, folder+"/f")
			gone = append(gone, "examples/"+e.ID)
		}
		err := open(t, fsys).Verify(release.Archived)
		if len(gone) == 0 {
			if err != nil {
				t.Fatalf("complete: %v", err)
			}
			continue
		}
		var inc *release.IncompleteError
		if !errors.As(err, &inc) {
			t.Fatalf("gone %v: err = %v", gone, err)
		}
		slices.Sort(gone)
		if !slices.Equal(inc.Missing, gone) {
			t.Fatalf("missing %v, want %v", inc.Missing, gone)
		}
	}
}

func TestACatalogueOfAnotherReleaseOrNoCatalogueDoesNotVerify(t *testing.T) {
	other, err := release.MarshalCatalogue(&pb.ReleaseCatalogue{Release: "2.0.0"})
	if err != nil {
		t.Fatal(err)
	}
	r := open(t, releasetest.Tree(t, dir, releasetest.Content(release.CatalogueName, string(other))))
	if err := r.Verify(release.Archived); err == nil {
		t.Fatal("a catalogue built for release 2.0.0 verified in release 1.0.0")
	}

	r = open(t, releasetest.Tree(t, dir, releasetest.Content(release.CatalogueName, "not a catalogue")))
	if err := r.Verify(release.Archived); err == nil {
		t.Fatal("garbage verified as a catalogue")
	}

	r = open(t, releasetest.Tree(t, dir, releasetest.Without(release.CatalogueName)))
	var inc *release.IncompleteError
	if err := r.Verify(release.Archived); !errors.As(err, &inc) || !slices.Equal(inc.Missing, []string{release.CatalogueName}) {
		t.Fatalf("err = %v", err)
	}
}

// The catalogue's rules refuse an entry that points outside what it says, so a catalogue cannot
// name a tree that is not the example's own.
func TestTheCatalogueRefusesEntriesThatDisagreeWithThemselves(t *testing.T) {
	good := func() *pb.ReleaseExample {
		return &pb.ReleaseExample{
			Example: "a/b", Folder: "b", Tree: "examples/a/b", Card: card("default"),
		}
	}
	cases := map[string]func(*pb.ReleaseExample){
		"tree elsewhere":      func(e *pb.ReleaseExample) { e.Tree = "bin/vibesys-host" },
		"folder not the last": func(e *pb.ReleaseExample) { e.Folder = "a" },
		"climbs":              func(e *pb.ReleaseExample) { e.Example, e.Tree = "../x", "examples/../x" },
		"absolute":            func(e *pb.ReleaseExample) { e.Example, e.Tree, e.Folder = "/x", "examples//x", "x" },
		"clone without flag":  func(e *pb.ReleaseExample) { e.Clone = &pb.CloneSource{Url: "u", Revision: pinned} },
		"flag without clone":  func(e *pb.ReleaseExample) { e.NeedsClone = true },
		"unpinned":            func(e *pb.ReleaseExample) { e.NeedsClone, e.Clone = true, &pb.CloneSource{Url: "u", Revision: "main"} },
		"option as url": func(e *pb.ReleaseExample) {
			e.NeedsClone, e.Clone = true, &pb.CloneSource{Url: "--upload-pack=x", Revision: pinned}
		},
		"no card": func(e *pb.ReleaseExample) { e.Card = nil },
	}
	if _, err := release.MarshalCatalogue(&pb.ReleaseCatalogue{Release: "1.0.0", Examples: []*pb.ReleaseExample{good()}}); err != nil {
		t.Fatalf("the good entry: %v", err)
	}
	for name, mutate := range cases {
		e := good()
		mutate(e)
		if _, err := release.MarshalCatalogue(&pb.ReleaseCatalogue{Release: "1.0.0", Examples: []*pb.ReleaseExample{e}}); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	if _, err := release.MarshalCatalogue(&pb.ReleaseCatalogue{Release: "1.0.0", Examples: []*pb.ReleaseExample{good(), good()}}); err == nil {
		t.Error("a task listed twice: accepted")
	}
}

// A catalogue's bytes parse back to the catalogue, and equal catalogues write equal bytes.
func TestACatalogueRoundTripsThroughItsBytes(t *testing.T) {
	rng := rand.New(rand.NewPCG(3, 5))
	for range 40 {
		c := &pb.ReleaseCatalogue{Release: "1.2.3-rc.1"}
		for i := range rng.IntN(8) {
			id := fmt.Sprintf("f%d/ex%d", rng.IntN(3), i)
			e := &pb.ReleaseExample{Example: id, Folder: fmt.Sprintf("ex%d", i), Tree: "examples/" + id, Card: card(fmt.Sprintf("t%d", rng.IntN(4)))}
			if rng.IntN(3) == 0 {
				e.NeedsClone, e.Clone = true, &pb.CloneSource{Url: "https://example.invalid/r.git", Revision: pinned}
			}
			c.Examples = append(c.Examples, e)
		}
		data, err := release.MarshalCatalogue(c)
		if err != nil {
			t.Fatal(err)
		}
		back, err := release.ParseCatalogue(data)
		if err != nil {
			t.Fatal(err)
		}
		again, err := release.MarshalCatalogue(back)
		if err != nil || string(again) != string(data) {
			t.Fatalf("not stable: %v", err)
		}
	}
}

func card(task string) *cliv1.TaskCard { return &cliv1.TaskCard{Task: task} }
