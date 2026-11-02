package archcheck

import (
	"fmt"
	"go/ast"
	"strings"
)

// A store is keyed by a core.Scope, which only core.ResolvedScope makes. A scope is made from a
// caller's directory only by resolving it through git, which app does in one file, and otherwise
// only by reading back a scope the daemon wrote after resolving it: the index of stores, and the
// launch record of a run. So no other product file may name ResolvedScope, and a store key is
// never a path a caller sent as it was.

// scopeMintAllowed lists the product files that may name core.ResolvedScope, with why.
var scopeMintAllowed = map[string]string{
	"cli/internal/app/scope.go":                 "resolves a caller's directory through gitrepo.Resolver, and reads back a run's launch record",
	"cli/internal/app/filestores/filestores.go": "reads back the index, which records only resolved scopes",
}

func scopeMintPermitted(name string) bool {
	_, listed := scopeMintAllowed[name]
	return listed || strings.HasSuffix(name, "_test.go") || strings.HasPrefix(name, "cli/internal/core/")
}

func scopeMints(name, source string) []string {
	if scopeMintPermitted(name) {
		return nil
	}
	file, err := parseFull(name, source)
	if err != nil {
		return []string{err.Error()}
	}
	var found []string
	ast.Inspect(file, func(n ast.Node) bool {
		if sel, ok := n.(*ast.SelectorExpr); ok && sel.Sel.Name == "ResolvedScope" {
			found = append(found, fmt.Sprintf("%s: names core.ResolvedScope, which only resolution and the reading back of a resolved scope may; take the core.CallContext that app.Resolve returned", name))
		}
		return true
	})
	return found
}
