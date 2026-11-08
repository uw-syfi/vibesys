package archcheck

import (
	"fmt"
	"go/ast"
	"go/token"
	"slices"
	"strconv"
	"strings"
)

// The names of a concrete placement kind, resource kind or vendor are spelled only by the code that
// implements it and by the decoding of the files that name it, so that no other code branches on a
// concrete name: everything else is handed an interface, or a value of a closed set, and the wiring
// in production connects them by importing the package. A string literal in product code that
// holds one of these spellings, in any case, outside the places listed for it, is refused. Import
// paths are not literals in this sense, so production may import the packages.

// spelledAt lists, for each spelling, the files and directories (ending in "/") that may hold it.
var spelledAt = map[string][]string{
	"kubernetes": {
		"cli/internal/resource/kubernetes/", "cli/internal/operator/",
		// The closed set of resource kinds: the one place the domain spells them.
		"cli/internal/core/placement.go",
	},
	// The apptainer placement is a Slurm cluster's: its engine and its cluster speak Slurm.
	"slurm":  {"cli/internal/environment/slurm/", "cli/internal/environment/apptainer/", "cli/internal/operator/"},
	// The accelerator registry spells the display names a task authors ("NVIDIA H100"), which are
	// the closed set of identities the domain knows, in the one file that holds them.
	"nvidia": {"cli/internal/backend/nvidia/", "cli/internal/resource/gpu/", "cli/internal/task/accelerator.go"},
}

// Spellings returns the spellings the rule holds, sorted.
func Spellings() []string {
	out := make([]string, 0, len(spelledAt))
	for s := range spelledAt {
		out = append(out, s)
	}
	slices.Sort(out)
	return out
}

func spellingAllowed(name, spelling string) bool {
	return slices.ContainsFunc(spelledAt[spelling], func(at string) bool {
		if strings.HasSuffix(at, "/") {
			return strings.HasPrefix(name, at)
		}
		return name == at
	})
}

// spellings reports each string literal of a product file under cli/ that spells a concrete name
// where it may not.
func spellings(name, source string) []string {
	if strings.HasSuffix(name, "_test.go") || strings.HasPrefix(name, genAt) || strings.HasPrefix(name, "cli/internal/archcheck/") {
		return nil
	}
	file, err := parseFull(name, source)
	if err != nil {
		return []string{err.Error()}
	}
	imports := map[*ast.BasicLit]bool{}
	for _, spec := range file.Imports {
		imports[spec.Path] = true
	}
	var found []string
	ast.Inspect(file, func(n ast.Node) bool {
		lit, ok := n.(*ast.BasicLit)
		if !ok || lit.Kind != token.STRING || imports[lit] {
			return true
		}
		text, err := strconv.Unquote(lit.Value)
		if err != nil {
			text = lit.Value
		}
		for _, spelling := range Spellings() {
			if strings.Contains(strings.ToLower(text), spelling) && !spellingAllowed(name, spelling) {
				found = append(found, fmt.Sprintf(
					"%s: a string spells %q, which only %s may: take a value of a closed set or an interface instead, and let production wire the package that implements it",
					name, spelling, strings.Join(spelledAt[spelling], ", ")))
			}
		}
		return true
	})
	return found
}
