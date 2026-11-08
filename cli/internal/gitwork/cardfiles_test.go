package gitwork_test

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/gitwork"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

const cardManifest = "version = \"0.2\"\n[catalogue]\ndomain = \"databases\"\n[catalogue.hardware]\naccelerator = \"CPU\"\ncount = 4\nvendor = \"cpu\"\n"

func cardTree() task.Tree {
	return task.Tree{
		".vibesys/tasks/t/" + task.ManifestName:  {Data: []byte(cardManifest)},
		".vibesys/tasks/t/" + task.ObjectiveName: {Data: []byte("[[objective]]\nname = \"qps\"\ndirection = \"max\"\n")},
		".vibesys/tasks/t/OBJECTIVE.md":          {Data: []byte("# Committed heading\n")},
		".vibesys/tasks/t/huge.bin":              {Data: []byte("not read")},
	}
}

func cardOf(t *testing.T, tree task.Tree, err error) (string, error) {
	t.Helper()
	if err != nil {
		return "", err
	}
	card, err := task.Card(tree, "t")
	if err != nil {
		return "", err
	}
	return card.GetHeading(), nil
}

// One task, three ways to reach it: committed, in the working tree and in a folder give the same card,
// and only the working tree sees an edit that is not committed.
func TestCardFilesAgreeAcrossSources(t *testing.T) {
	repo, _ := newRepo(t, cardTree())
	ctx := context.Background()
	var c gitwork.CardFiles
	read := func(where string) *string {
		var tree task.Tree
		var err error
		switch where {
		case "head":
			tree, err = c.Committed(ctx, repo, "", "t")
		case "branch":
			tree, err = c.Committed(ctx, repo, "main", "t")
		case "tree":
			tree, err = c.WorkingTree(ctx, repo, "t")
		case "folder":
			tree, err = c.Folder(ctx, repo, "t")
		}
		h, err := cardOf(t, tree, err)
		if err != nil {
			t.Fatalf("%s: %v", where, err)
		}
		return &h
	}
	for _, where := range []string{"head", "branch", "tree", "folder"} {
		if got := *read(where); got != "Committed heading" {
			t.Errorf("%s: heading %q", where, got)
		}
	}
	if err := os.WriteFile(filepath.Join(repo, ".vibesys/tasks/t/OBJECTIVE.md"), []byte("# Draft heading\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	for where, want := range map[string]string{"head": "Committed heading", "branch": "Committed heading", "tree": "Draft heading", "folder": "Draft heading"} {
		if got := *read(where); got != want {
			t.Errorf("after an edit, %s: heading %q, want %q", where, got, want)
		}
	}
	tree, err := c.Committed(ctx, repo, "", "t")
	if err != nil || len(tree) != 3 {
		t.Errorf("committed files = %d, %v; want only the three of a card", len(tree), err)
	}
	a, _ := task.Card(tree, "t")
	folder, err := c.Folder(ctx, repo, "t")
	if err != nil {
		t.Fatal(err)
	}
	b, _ := task.Card(folder, "t")
	if proto.Equal(a, b) {
		t.Error("the edit should make the cards differ")
	}
}

// Not found is not found, a revision cannot be an option to git, and a task that is not a task name is
// refused, for every source.
func TestCardFilesNameWhatIsMissingOrRefused(t *testing.T) {
	repo, _ := newRepo(t, cardTree())
	ctx := context.Background()
	var c gitwork.CardFiles
	kind := func(err error) core.FailureKind {
		var f *core.Failure
		if !errors.As(err, &f) {
			t.Fatalf("not a Failure: %v", err)
		}
		return f.Kind
	}
	if _, err := c.Committed(ctx, repo, "no-such-branch", "t"); kind(err) != core.FailureNotFound {
		t.Errorf("unknown revision: %v", err)
	}
	if _, err := c.Committed(ctx, t.TempDir(), "", "t"); kind(err) != core.FailureNotFound {
		t.Errorf("not a repository: %v", err)
	}
	for _, rev := range []string{"--output=/tmp/x", "-1", "a\nb"} {
		if _, err := c.Committed(ctx, repo, rev, "t"); kind(err) != core.FailureInvalidInput {
			t.Errorf("revision %q: %v", rev, err)
		}
	}
	for _, name := range []string{"..", "a/b", ""} {
		if _, err := c.Committed(ctx, repo, "", name); kind(err) != core.FailureInvalidInput {
			t.Errorf("committed task %q: %v", name, err)
		}
		if _, err := c.WorkingTree(ctx, repo, name); kind(err) != core.FailureInvalidInput {
			t.Errorf("working tree task %q: %v", name, err)
		}
		if _, err := c.Folder(ctx, repo, name); kind(err) != core.FailureInvalidInput {
			t.Errorf("folder task %q: %v", name, err)
		}
	}
	if _, err := c.Folder(ctx, filepath.Join(repo, "absent"), "t"); kind(err) != core.FailureNotFound {
		t.Errorf("absent folder: %v", err)
	}
	if tree, err := c.Folder(ctx, repo, "absent"); err != nil || len(tree) != 0 {
		t.Errorf("a task with no files is an empty tree: %v, %v", tree, err)
	}
	empty, _ := c.Folder(ctx, repo, "absent")
	if _, err := task.Card(empty, "absent"); kind(err) != core.FailureNotFound {
		t.Errorf("card of an empty tree: %v", err)
	}
}

// A file of the task that is a symbolic link is refused where it is committed and where it is checked
// out, and never read through, so a task cannot make the daemon read a file outside it.
func TestCardFilesRefuseALinkWithoutFollowingIt(t *testing.T) {
	secret := filepath.Join(t.TempDir(), "secret")
	if err := os.WriteFile(secret, []byte("# Not for you\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	files := cardTree()
	files[".vibesys/tasks/t/OBJECTIVE.md"] = task.File{Link: secret}
	repo, _ := newRepo(t, files)
	ctx := context.Background()
	var c gitwork.CardFiles
	for name, call := range map[string]func() (task.Tree, error){
		"committed":    func() (task.Tree, error) { return c.Committed(ctx, repo, "", "t") },
		"working tree": func() (task.Tree, error) { return c.WorkingTree(ctx, repo, "t") },
		"folder":       func() (task.Tree, error) { return c.Folder(ctx, repo, "t") },
	} {
		tree, err := call()
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "OBJECTIVE.md") || tree != nil {
			t.Errorf("%s: %v, %v; want invalid input naming the file", name, tree, err)
		}
	}
}

// A file over the bound is refused, not read in part.
func TestCardFilesRefuseAFileOverTheBound(t *testing.T) {
	files := cardTree()
	files[".vibesys/tasks/t/OBJECTIVE.md"] = task.File{Data: []byte(strings.Repeat("x", gitwork.MaxCardFileBytes+1))}
	repo, _ := newRepo(t, files)
	var c gitwork.CardFiles
	for name, call := range map[string]func() (task.Tree, error){
		"committed": func() (task.Tree, error) { return c.Committed(context.Background(), repo, "", "t") },
		"folder":    func() (task.Tree, error) { return c.Folder(context.Background(), repo, "t") },
	} {
		if _, err := call(); err == nil || core.AsFailure(err).Kind != core.FailureInvalidInput {
			t.Errorf("%s: %v", name, err)
		}
	}
}
