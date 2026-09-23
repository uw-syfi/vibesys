package main

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func FuzzParseNameStatusZ(f *testing.F) {
	for _, seed := range [][]byte{
		{},
		[]byte("M\x00file.go\x00"),
		[]byte("A\x00new file\twith\nlines\x00D\x00--old-name\x00"),
		[]byte("R100\x00old name\x00new name\x00"),
		[]byte("C075\x00source\x00copy\x00"),
		[]byte("M\x00same\x00A\x00same\x00"),
		[]byte("R100\x00only-old\x00"),
		[]byte("Rxyz\x00old\x00new\x00"),
		[]byte("M\x00unterminated"),
		[]byte("M\x00\x00"),
	} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, raw []byte) {
		if len(raw) > 4096 {
			t.Skip()
		}
		paths, err := parseNameStatusZ(raw)
		if err != nil {
			return
		}

		seen := make(map[string]bool, len(paths))
		for _, path := range paths {
			if path == "" {
				t.Fatal("parser returned an empty path")
			}
			if seen[path] {
				t.Fatalf("parser returned duplicate path %q", path)
			}
			seen[path] = true
		}
	})
}

func TestParseNameStatusZPreservesPathsAndDeduplicates(t *testing.T) {
	raw := []byte("M\x00tab\tand\nnewline\x00R100\x00--old\x00new\x00A\x00new\x00")
	got, err := parseNameStatusZ(raw)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"tab\tand\nnewline", "--old", "new"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("parseNameStatusZ() = %#v, want %#v", got, want)
	}
}

func TestParseNameStatusZRejectsMalformedRecords(t *testing.T) {
	for _, raw := range [][]byte{
		[]byte("M\x00path"),
		[]byte("R100\x00old\x00"),
		[]byte("Rxyz\x00old\x00new\x00"),
		[]byte("R101\x00old\x00new\x00"),
		[]byte("?\x00path\x00"),
		[]byte("M\x00\x00"),
	} {
		if _, err := parseNameStatusZ(raw); err == nil {
			t.Errorf("parseNameStatusZ(%q) succeeded, want an error", raw)
		}
	}
}

func TestWorktreePathsIncludesStagedUnstagedUntrackedAndRename(t *testing.T) {
	root := t.TempDir()
	initGitRepo(t, root)
	for _, name := range []string{"staged.txt", "unstaged.txt", "old.txt", "deleted.txt"} {
		writeFixtureFile(t, root, name, "original\n")
	}
	commitFixture(t, root, "initial")
	writeFixtureFile(t, root, "staged.txt", "staged\n")
	gitTest(t, root, "add", "staged.txt")
	writeFixtureFile(t, root, "unstaged.txt", "unstaged\n")
	gitTest(t, root, "mv", "old.txt", "renamed.txt")
	if err := os.Remove(filepath.Join(root, "deleted.txt")); err != nil {
		t.Fatal(err)
	}
	writeFixtureFile(t, root, "untracked.txt", "new\n")

	got, err := worktreePaths(root)
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]bool{
		"staged.txt": true, "unstaged.txt": true, "old.txt": true,
		"renamed.txt": true, "deleted.txt": true, "untracked.txt": true,
	}
	if len(got) != len(want) {
		t.Fatalf("worktree paths = %q, want %v", got, want)
	}
	for _, path := range got {
		if !want[path] {
			t.Fatalf("unexpected worktree path %q in %q", path, got)
		}
	}
	if got := appendUniquePaths([]string{"staged.txt", "committed.txt"}, got); len(got) != len(want)+1 {
		t.Fatalf("combined paths contain duplicates: %q", got)
	}
}
