package propcheck_test

import (
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"path/filepath"
	"strings"
	"testing"
)

// TestQuickChecksUseConfig fails on a quick.Check in a _test.go file of this module whose config
// is not a call of propcheck.Config, since any other config draws a new seed on every run.
func TestQuickChecksUseConfig(t *testing.T) {
	fset := token.NewFileSet()
	err := filepath.WalkDir("../..", func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() || !strings.HasSuffix(path, "_test.go") {
			return err
		}
		file, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			return err
		}
		ast.Inspect(file, func(n ast.Node) bool {
			call, ok := n.(*ast.CallExpr)
			if !ok || !isSelector(call.Fun, "quick", "Check") {
				return true
			}
			if len(call.Args) != 2 || !isConfigCall(call.Args[1]) {
				t.Errorf("%s: quick.Check must take propcheck.Config(t, base) as its config", fset.Position(call.Pos()))
			}
			return true
		})
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

func isSelector(e ast.Expr, pkg, name string) bool {
	sel, ok := e.(*ast.SelectorExpr)
	if !ok || sel.Sel.Name != name {
		return false
	}
	id, ok := sel.X.(*ast.Ident)
	return ok && id.Name == pkg
}

func isConfigCall(e ast.Expr) bool {
	call, ok := e.(*ast.CallExpr)
	return ok && isSelector(call.Fun, "propcheck", "Config")
}
