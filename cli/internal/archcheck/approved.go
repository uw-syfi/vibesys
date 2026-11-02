package archcheck

import (
	"fmt"
	"go/ast"
	"strings"
)

// A promotion is recorded only with the proof that a decision approved it (core.Approved, whose
// proof is a private field). core.AssumeApproved makes that proof without a decision, for tests of
// stores that hold promotions no decision would make. Product code may not name it, so the proof
// stays one only DecidePromotion makes.

// approvalMintAllowed says whether a file may name core.AssumeApproved: the package that defines
// it, the contract suite for stores (imported by test files only), and any test file.
func approvalMintAllowed(name string) bool {
	return strings.HasSuffix(name, "_test.go") ||
		strings.HasPrefix(name, "cli/internal/core/") ||
		strings.HasPrefix(name, "cli/internal/store/storetest/")
}

func approvalMints(name, source string) []string {
	if approvalMintAllowed(name) {
		return nil
	}
	file, err := parseFull(name, source)
	if err != nil {
		return []string{err.Error()}
	}
	var found []string
	ast.Inspect(file, func(n ast.Node) bool {
		if sel, ok := n.(*ast.SelectorExpr); ok && sel.Sel.Name == "AssumeApproved" {
			found = append(found, fmt.Sprintf("%s: names AssumeApproved, which makes a promotion's approval without a decision and is for tests; record the Approved that DecidePromotion returned", name))
		}
		return true
	})
	return found
}
