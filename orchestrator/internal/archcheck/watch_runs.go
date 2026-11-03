package archcheck

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"strings"
)

// GetEvent is ambiguous on WatchRunsResponse. Keep event projection in the checked decoder.
func watchRunsReaders(name, source string) []string {
	if !strings.HasPrefix(name, moduleDir+"internal/") {
		return nil
	}
	positions := token.NewFileSet()
	file, err := parser.ParseFile(positions, name, source, 0)
	if err != nil {
		return nil
	} // layering reports parse errors.
	var found []string
	ast.Inspect(file, func(node ast.Node) bool {
		selector, ok := node.(*ast.SelectorExpr)
		if ok && selector.Sel.Name == "GetEvent" {
			found = append(found, fmt.Sprintf("%s:%d: use client.DecodeWatchRuns instead of GetEvent so every oneof kind is classified", name, positions.Position(selector.Pos()).Line))
		}
		return true
	})
	return found
}
