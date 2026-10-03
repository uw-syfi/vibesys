package main

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

// Language adapter registrations belong in the two composition files. Shared
// policy, discovery, and execution code must not name or import adapters.
func TestSharedCodeHasNoLanguageSpecificBranches(t *testing.T) {
	for _, dir := range []string{".", "execution", "discovery"} {
		entries, err := os.ReadDir(dir)
		if err != nil {
			t.Fatal(err)
		}
		for _, entry := range entries {
			name := entry.Name()
			if entry.IsDir() || !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
				continue
			}
			path := filepath.Join(dir, name)
			if path == "discovery_adapters.go" || path == "test_planners.go" {
				continue
			}
			t.Run(path, func(t *testing.T) { checkSharedFile(t, path) })
		}
	}
}

func checkSharedFile(t *testing.T, path string) {
	t.Helper()
	file, err := parser.ParseFile(token.NewFileSet(), path, nil, 0)
	if err != nil {
		t.Fatal(err)
	}
	for _, spec := range file.Imports {
		name, err := strconv.Unquote(spec.Path.Value)
		if err != nil {
			t.Fatal(err)
		}
		if strings.HasPrefix(name, "repoctl/execution/") || strings.HasPrefix(name, "repoctl/discovery/") {
			t.Errorf("shared file imports adapter %q", name)
		}
	}
	ast.Inspect(file, func(node ast.Node) bool {
		switch n := node.(type) {
		case *ast.BasicLit:
			if n.Kind != token.STRING {
				break
			}
			value, err := strconv.Unquote(n.Value)
			if err != nil {
				t.Error(err)
			}
			if isLanguageName(value) {
				t.Errorf("shared file names language %q", value)
			}
		case *ast.BinaryExpr:
			if (n.Op == token.EQL || n.Op == token.NEQ) &&
				(isNonemptyLanguageComparison(n.X, n.Y) || isNonemptyLanguageComparison(n.Y, n.X)) {
				t.Error("shared file compares a language to an implementation")
			}
		case *ast.SwitchStmt:
			if isLanguageSelector(n.Tag) {
				t.Error("shared file switches on language")
			}
		}
		return true
	})
}

func isLanguageName(value string) bool {
	_, registered := testPlanners[value]
	return registered
}

func isNonemptyLanguageComparison(left, right ast.Expr) bool {
	if !isLanguageSelector(left) {
		return false
	}
	lit, ok := right.(*ast.BasicLit)
	if !ok || lit.Kind != token.STRING {
		return true
	}
	value, err := strconv.Unquote(lit.Value)
	return err != nil || value != ""
}

func isLanguageSelector(expr ast.Expr) bool {
	selector, ok := expr.(*ast.SelectorExpr)
	return ok && selector.Sel.Name == "Language"
}
