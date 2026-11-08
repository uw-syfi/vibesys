package archcheck_test

import (
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// The race pass of check.sh runs every test of a package again under the race detector, at several
// times the cost, and can only find a race where the code or its tests run a goroutine. So a package
// is in it exactly when it has a go statement, in its code or its tests, and a package without one is
// not: the detector finds nothing in a single thread, and mcp, search and hub were in the pass for
// minutes of the check.
func TestTheRacePassHoldsEveryPackageThatStartsAGoroutineAndNoOther(t *testing.T) {
	script, err := os.ReadFile("../../check.sh")
	if err != nil {
		t.Fatal(err)
	}
	var raced []string
	for _, line := range strings.Split(string(script), "\n") {
		if !strings.HasPrefix(line, "go test") || !strings.Contains(line, " -race ") {
			continue
		}
		for _, word := range strings.Fields(line) {
			if strings.HasPrefix(word, "./") {
				raced = append(raced, strings.TrimSuffix(strings.TrimPrefix(word, "./"), "/"))
			}
		}
	}
	if len(raced) == 0 {
		t.Fatal("check.sh has no race pass")
	}
	// within says whether the package directory dir is one the race argument r names.
	within := func(r, dir string) bool {
		if prefix, tree := strings.CutSuffix(r, "/..."); tree {
			return dir == prefix || strings.HasPrefix(dir, prefix+"/")
		}
		return dir == r
	}
	starts := map[string]bool{}
	err = filepath.WalkDir("../..", func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() || !strings.HasSuffix(path, ".go") {
			return err
		}
		file, err := parser.ParseFile(token.NewFileSet(), path, nil, 0)
		if err != nil {
			return err
		}
		ast.Inspect(file, func(n ast.Node) bool {
			if _, ok := n.(*ast.GoStmt); ok {
				dir, _ := filepath.Rel("../..", filepath.Dir(path))
				dir = filepath.ToSlash(dir)
				// A package of test support runs in the tests of the package it serves.
				if strings.HasSuffix(dir, "test") {
					dir = filepath.ToSlash(filepath.Dir(dir))
				}
				starts[dir] = true
			}
			return true
		})
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	for dir := range starts {
		covered := false
		for _, r := range raced {
			covered = covered || within(r, dir)
		}
		if !covered {
			t.Errorf("%s starts a goroutine and is not in a race pass of orchestrator/check.sh", dir)
		}
	}
	for _, r := range raced {
		found := false
		for dir := range starts {
			found = found || within(r, dir)
		}
		if !found {
			t.Errorf("%s is in a race pass of orchestrator/check.sh and starts no goroutine, so the detector has nothing to find there", r)
		}
	}
}
