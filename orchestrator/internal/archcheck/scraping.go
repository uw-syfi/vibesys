package archcheck

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"strconv"
	"strings"
)

// scrapingSpellings are the ways product code learns about another process by reading the machine's
// process table: the tools that print it and the per-process command line file. A process says who
// it is through a typed service (HostControlService for the host) or the lock it holds, so reading
// another process's command line is a second, guessing definition of that fact.
var scrapingSpellings = []string{"pgrep", "pkill", "/cmdline"}

// scrapingTools are the whole-literal spellings that name a process-listing program.
var scrapingTools = map[string]bool{"ps": true}

// scrapingAllowed are the files that still read a process command line, with the number of
// literals. The session holder's pid check is the one left; it ends when the session process says
// who it is through its own typed contract.
var scrapingAllowed = map[string]int{
	moduleDir + "internal/production/sidecars.go": 1,
}

// processScraping reports a string literal in product code that names a process-table tool or a
// process command line file, beyond what scrapingAllowed holds.
func processScraping(name, source string) []string {
	if strings.HasPrefix(name, genDir) || strings.HasPrefix(name, moduleDir+"internal/archcheck/") || strings.HasSuffix(name, "_test.go") {
		return nil
	}
	file, err := parser.ParseFile(token.NewFileSet(), name, source, 0)
	if err != nil {
		return nil // layering reports it.
	}
	count := 0
	var spelled string
	ast.Inspect(file, func(n ast.Node) bool {
		lit, ok := n.(*ast.BasicLit)
		if !ok || lit.Kind != token.STRING {
			return true
		}
		text, err := strconv.Unquote(lit.Value)
		if err != nil {
			return true
		}
		if scrapingTools[text] {
			count++
			spelled = text
		}
		for _, s := range scrapingSpellings {
			if strings.Contains(text, s) {
				count++
				spelled = s
			}
		}
		return true
	})
	if count == scrapingAllowed[name] {
		return nil
	}
	if count == 0 {
		return []string{fmt.Sprintf("%s: reads %d process-table spellings, but the allowance says %d; shrink scrapingAllowed", name, count, scrapingAllowed[name])}
	}
	return []string{fmt.Sprintf("%s: spells %q, reading another process's identity from the process table (%d spellings, %d allowed); ask the process through its typed service or lock instead", name, spelled, count, scrapingAllowed[name])}
}
