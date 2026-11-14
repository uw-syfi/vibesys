package production

import (
	"context"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"testing/fstest"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/release"
	"github.com/uw-syfi/vibesys/orchestrator/release/assemble"
	"github.com/uw-syfi/vibesys/orchestrator/release/releasetest"
)

const examplePin = "0123456789abcdef0123456789abcdef01234567"

type externalsOf map[string]fs.FS

func (e externalsOf) Fetch(_ context.Context, entry assemble.Entry) (fs.FS, assemble.Pin, error) {
	return e[entry.Path], assemble.Pin{URL: "https://example.invalid/" + filepath.Base(entry.Path) + ".git", Revision: examplePin}, nil
}

type headingCards struct{}

func (headingCards) Card(_ context.Context, folder, task string) (*cliv1.TaskCard, error) {
	return &cliv1.TaskCard{Task: task, Heading: filepath.Base(folder) + "/" + task}, nil
}

func file(text string) *fstest.MapFile { return &fstest.MapFile{Data: []byte(text), Mode: 0o644} }

// builtRelease is a release on disk whose examples and catalogue the builder made, and its Finder.
func builtRelease(t *testing.T) release.Finder {
	t.Helper()
	repo := fstest.MapFS{
		"examples/db/kv/.vibesys/tasks/get/vibesys.input.toml": file("g"),
		"examples/db/kv/.vibesys/tasks/put/vibesys.input.toml": file("p"),
		"examples/db/kv/src/lib.rs":                            file("lib"),
		// What a machine that cloned the submodule has: never shipped.
		"examples/ms/repositories/vllm/vllm/engine.py": file("source"),
	}
	external := externalsOf{"examples/ms/repositories/vllm": fstest.MapFS{".vibesys/tasks/serve/vibesys.input.toml": file("s")}}
	entries := []assemble.Entry{{Path: "examples/db/kv"}, {Path: "examples/ms/repositories/vllm", External: true}}

	dir := t.TempDir()
	staged, err := assemble.Stage(t.Context(), repo, entries, external, assemble.DirSink{Root: dir})
	if err != nil {
		t.Fatal(err)
	}
	catalogue, err := assemble.Catalogue(t.Context(), "1.0.0", dir, staged, headingCards{})
	if err != nil {
		t.Fatal(err)
	}
	data, err := release.MarshalCatalogue(catalogue)
	if err != nil {
		t.Fatal(err)
	}
	releasetest.Write(t, dir, releasetest.Content(release.CatalogueName, string(data)))
	return func() (*release.Release, error) { return release.OpenDir(dir) }
}

func TestTheCatalogueSourceServesTheBuiltReleasesTasksWithoutFit(t *testing.T) {
	source := ReleaseCatalogue{Find: builtRelease(t), RepositoriesDirectory: "/home/u/repos"}
	got, err := source.Catalogue(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if got.Release != "1.0.0" || got.RepositoriesDirectory != "/home/u/repos" {
		t.Fatalf("catalogue = %+v", got)
	}
	var entries []string
	for _, e := range got.Entries {
		entries = append(entries, e.GetExample()+":"+e.GetCard().GetTask()+":"+e.GetCard().GetHeading())
		if e.GetFit() != nil {
			t.Errorf("%s has a fit before the daemon works one out", e.GetExample())
		}
		if e.GetNeedsClone() != (e.GetExample() == "ms/repositories/vllm") {
			t.Errorf("%s: needs_clone = %v", e.GetExample(), e.GetNeedsClone())
		}
	}
	want := []string{"db/kv:get:kv/get", "db/kv:put:kv/put", "ms/repositories/vllm:serve:vllm/serve"}
	if !slices.Equal(entries, want) {
		t.Fatalf("entries %v, want %v", entries, want)
	}
}

func TestTheExampleSourceReadsTheBuiltTreesAndPinsClones(t *testing.T) {
	trees := ReleaseExamples{Find: builtRelease(t)}
	own, err := trees.Source(t.Context(), "db/kv")
	if err != nil || own.Clone != nil {
		t.Fatalf("db/kv: %+v, %v", own, err)
	}
	if b, err := fs.ReadFile(own.Vendored, "src/lib.rs"); err != nil || string(b) != "lib" {
		t.Fatalf("src/lib.rs = %q, %v", b, err)
	}
	cloned, err := trees.Source(t.Context(), "ms/repositories/vllm")
	if err != nil {
		t.Fatal(err)
	}
	if cloned.Clone == nil || *cloned.Clone != (daemon.CloneSpec{URL: "https://example.invalid/vllm.git", Revision: examplePin}) {
		t.Fatalf("clone = %+v", cloned.Clone)
	}
	if _, err := fs.Stat(cloned.Vendored, "vllm/engine.py"); err == nil {
		t.Fatal("the external repository's source is in the release")
	}
	if _, err := fs.Stat(cloned.Vendored, ".vibesys/tasks/serve/vibesys.input.toml"); err != nil {
		t.Fatal(err)
	}
	var gone *daemon.ExampleUnavailable
	if _, err := trees.Source(t.Context(), "db/nothing"); !errors.As(err, &gone) || gone.Reason != daemon.ExampleNotFound || gone.Detail != "db/nothing" {
		t.Fatalf("an example the release lacks: %v", err)
	}
}

func TestTheSourcesFailAsTheReleaseDoes(t *testing.T) {
	got, err := ReleaseCatalogue{Find: developerBuild, RepositoriesDirectory: "/home/u/repos"}.Catalogue(t.Context())
	var notInstalled *release.NotInstalledError
	if !errors.As(err, &notInstalled) {
		t.Fatalf("catalogue of a developer build: %v", err)
	}
	// The directory survives the failure: the daemon reports it beside a classified problem rather
	// than losing it, the one fact ListCatalogue still has to give the client.
	if got.RepositoriesDirectory != "/home/u/repos" {
		t.Fatalf("repositories directory on a failed catalogue = %q", got.RepositoriesDirectory)
	}
	if _, err := (ReleaseExamples{Find: developerBuild}).Source(t.Context(), "a/b"); !errors.As(err, &notInstalled) {
		t.Fatalf("source of a developer build: %v", err)
	}
	// A release whose catalogue file is gone is incomplete, naming it.
	dir := t.TempDir()
	releasetest.Write(t, dir)
	if err := os.Remove(filepath.Join(dir, release.CatalogueName)); err != nil {
		t.Fatal(err)
	}
	find := func() (*release.Release, error) { return release.OpenDir(dir) }
	var incomplete *release.IncompleteError
	gotIncomplete, err := (ReleaseCatalogue{Find: find, RepositoriesDirectory: "/home/u/repos"}).Catalogue(t.Context())
	if !errors.As(err, &incomplete) || !slices.Equal(incomplete.Missing, []string{release.CatalogueName}) {
		t.Fatalf("catalogue of an incomplete release: %v", err)
	}
	if gotIncomplete.RepositoriesDirectory != "/home/u/repos" {
		t.Fatalf("repositories directory on an incomplete catalogue = %q", gotIncomplete.RepositoriesDirectory)
	}
}

// Problem turns the two release errors a client can act on into the matching reason, naming the cause,
// and leaves every other failure to the opaque Unavailable by answering nil.
func TestTheCatalogueSourceClassifiesTheProblemsAClientCanActOn(t *testing.T) {
	source := ReleaseCatalogue{}
	noRelease := source.Problem(&release.NotInstalledError{Program: "/opt/vibesys/bin/vibesys-host", Manifest: "/opt/vibesys/" + release.ManifestName})
	if noRelease.GetReason() != pb.CatalogueProblemReason_CATALOGUE_PROBLEM_REASON_NO_RELEASE {
		t.Errorf("no release: reason %v", noRelease.GetReason())
	}
	if !strings.Contains(noRelease.GetDetail(), release.ManifestName) {
		t.Errorf("no release: detail %q does not name %s", noRelease.GetDetail(), release.ManifestName)
	}
	incomplete := source.Problem(&release.IncompleteError{Dir: "/opt/vibesys", Missing: []string{release.CatalogueName}})
	if incomplete.GetReason() != pb.CatalogueProblemReason_CATALOGUE_PROBLEM_REASON_RELEASE_INCOMPLETE {
		t.Errorf("incomplete release: reason %v", incomplete.GetReason())
	}
	if other := source.Problem(errors.New("the catalogue.pb is corrupt")); other != nil {
		t.Errorf("an unclassified failure: got %v, want nil", other)
	}
}
