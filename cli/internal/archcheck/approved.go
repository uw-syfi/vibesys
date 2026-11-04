package archcheck

import (
	"fmt"
	"go/ast"
	"strings"
)

// A promotion is recorded only with the proof that a decision approved it (core.Approved, whose
// proof is a private field), and a frontier changes only with the proof that a decision admitted a
// commit (core.FrontierAdmission). core.AssumeApproved and core.AssumeAdmitted make those proofs
// without a decision, for tests of stores that hold promotions and frontiers no decision would
// make. Product code may not name them, so each proof stays one only its decision makes.

// proofMints names each function that makes a proof without a decision, with what to use instead.
var proofMints = map[string]string{
	"AssumeApproved": "makes a promotion's approval without a decision and is for tests; record the Approved that DecidePromotion returned",
	"AssumeAdmitted": "makes a frontier admission without a decision and is for tests; record the FrontierAdmission that DecideFrontier returned",
}

// approvalMintAllowed says whether a file may name a proof mint: the package that defines them,
// the contract suite for stores (imported by test files only), and any test file.
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
		if sel, ok := n.(*ast.SelectorExpr); ok {
			if why, mint := proofMints[sel.Sel.Name]; mint {
				found = append(found, fmt.Sprintf("%s: names %s, which %s", name, sel.Sel.Name, why))
			}
		}
		return true
	})
	return found
}
