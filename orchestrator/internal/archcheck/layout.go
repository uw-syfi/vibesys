package archcheck

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"strconv"
	"strings"
)

// releaseLayoutSpellings are the spellings of the release layout that only package release may
// write: the manifest's file name and the session holder's environment folder. Any other program
// learns where a part is from the manifest, so a second spelling is a second definition that can
// drift (the layout used to be written in four places).
var releaseLayoutSpellings = []string{"release.json", ".venv"}

// releaseOwner is the directory that owns the release layout.
const releaseOwner = moduleDir + "release/"

// layoutLiterals reports a string literal in product code that spells the release layout, outside
// the package that owns it.
func layoutLiterals(name, source string) []string {
	// The check names the spellings it looks for, so its own package is not looked at.
	if strings.HasPrefix(name, genDir) || strings.HasPrefix(name, releaseOwner) || strings.HasPrefix(name, moduleDir+"internal/archcheck/") || strings.HasSuffix(name, "_test.go") {
		return nil
	}
	return literalsSpelling(name, source, releaseLayoutSpellings)
}

// literalsSpelling reports each string literal of a Go source that contains one of spellings.
func literalsSpelling(name, source string, spellings []string) []string {
	file, err := parser.ParseFile(token.NewFileSet(), name, source, 0)
	if err != nil {
		return nil // layering reports it.
	}
	var found []string
	ast.Inspect(file, func(n ast.Node) bool {
		lit, ok := n.(*ast.BasicLit)
		if !ok || lit.Kind != token.STRING {
			return true
		}
		text, err := strconv.Unquote(lit.Value)
		if err != nil {
			return true
		}
		for _, s := range spellings {
			if strings.Contains(text, s) {
				found = append(found, fmt.Sprintf("%s: spells the release layout (%q); only package release may, and everything else reads release.json through it", name, s))
			}
		}
		return true
	})
	return found
}
