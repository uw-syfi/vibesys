package archcheck

import (
	"fmt"
	"go/ast"
	"go/token"
	"go/types"
	"strings"
)

// WatchRunsReaders checks resolved types, including aliases and method values. Writers may
// construct frames, but only the decoder may project their body or inspect wrapper types.
// Interface erasure, generics and ProtoReflect are not detected.
func WatchRunsReaders(name string, file *ast.File, positions *token.FileSet, info *types.Info) []string {
	if name == "orchestrator/internal/client/watch_runs.go" || strings.Contains(name, "/gen/") {
		return nil
	}
	var found []string
	report := func(node ast.Node) {
		found = append(found, fmt.Sprintf("%s:%d: use client.DecodeWatchRuns to classify every WatchRunsResponse kind", name, positions.Position(node.Pos()).Line))
	}
	ast.Inspect(file, func(node ast.Node) bool {
		switch n := node.(type) {
		case *ast.SelectorExpr:
			selection := info.Selections[n]
			if selection == nil {
				break
			}
			owner := watchRunsOwner(selection)
			// A defined copy of the response still holds the generated oneof interface.
			if strings.HasPrefix(watchRunsType(selection.Obj().Type()), "isWatchRunsResponse_") {
				report(n)
				break
			}
			if owner == "WatchRunsResponse" {
				switch n.Sel.Name {
				case "Body", "Change", "Snapshot", "Search", "GetBody", "GetChange", "GetSnapshot", "GetSearch":
					report(n)
				}
			} else if strings.HasPrefix(owner, "WatchRunsResponse_") {
				report(n)
			}
		case *ast.TypeAssertExpr:
			if n.Type != nil && strings.HasPrefix(watchRunsType(info.TypeOf(n.Type)), "WatchRunsResponse_") {
				report(n)
			}
		case *ast.CaseClause:
			for _, expr := range n.List {
				if strings.HasPrefix(watchRunsType(info.TypeOf(expr)), "WatchRunsResponse_") {
					report(expr)
				}
			}
		}
		return true
	})
	return found
}

func watchRunsType(t types.Type) string {
	if t == nil {
		return ""
	}
	t = types.Unalias(t)
	if ptr, ok := t.(*types.Pointer); ok {
		t = types.Unalias(ptr.Elem())
	}
	named, ok := t.(*types.Named)
	if !ok || named.Obj().Pkg() == nil || named.Obj().Pkg().Path() != modulePath+"gen/vibesys/orchestrator/v1" {
		return ""
	}
	return named.Obj().Name()
}

// Resolve promoted selections to their declaring receiver, so embedding a response cannot
// bypass the guard and an unrelated message with the same method stays legal.
func watchRunsOwner(selection *types.Selection) string {
	if method, ok := selection.Obj().(*types.Func); ok {
		signature := method.Type().(*types.Signature)
		return watchRunsType(signature.Recv().Type())
	}
	owner := selection.Recv()
	for _, index := range selection.Index()[:len(selection.Index())-1] {
		owner = types.Unalias(owner)
		if pointer, ok := owner.(*types.Pointer); ok {
			owner = pointer.Elem()
		}
		structure, ok := owner.Underlying().(*types.Struct)
		if !ok {
			return ""
		}
		owner = structure.Field(index).Type()
	}
	return watchRunsType(owner)
}
