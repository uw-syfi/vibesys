// Package archcheck enforces the host's composition boundary over supplied Go sources.
package archcheck

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"path"
	"slices"
	"sort"
	"strconv"
	"strings"
)

// CommandDirectory is the host's command path, changed when the host is renamed.
const CommandDirectory = "gateway/cmd/vibesys-host"

// ClientDirectory is the client's command path.
const ClientDirectory = "gateway/cmd/vibesys-client"

const module = "github.com/uw-syfi/vibesys/"

// command is one binary of the module: a directory with a single main file that imports only os
// and its composition package, which in turn may import only what allowed lists.
type command struct {
	directory, composition string
	allowed                []string
}

var commands = []command{
	{CommandDirectory, "gateway/internal/hostmain", []string{
		module + "gateway/internal/buildinfo", module + "gateway/internal/connect", module + "gateway/internal/lifecycle",
		module + "gateway/internal/pipe", module + "gateway/internal/server",
		module + "gateway/schemahash", module + "orchestrator/gatewayroot", module + "orchestrator/hostconfig", module + "orchestrator/hosted", module + "orchestrator/hostlock",
	}},
	{ClientDirectory, "gateway/internal/clientmain", []string{
		module + "gateway/internal/buildinfo", module + "gateway/internal/connect", module + "gateway/internal/lifecycle",
		module + "orchestrator/clientcli", module + "orchestrator/gatewayroot", module + "orchestrator/gen/vibesys/orchestrator/v1", module + "orchestrator/hostconfig", module + "orchestrator/hostlock", module + "orchestrator/release",
		"github.com/mattn/go-isatty",
	}},
}

// Violations returns sorted diagnostics for the command shapes and composition imports.
// Files use repository-relative, slash-separated paths; tests are unrestricted.
func Violations(files map[string]string) []string {
	var found []string
	for _, c := range commands {
		found = append(found, c.violations(files)...)
	}
	for name, source := range files {
		if strings.HasPrefix(name, "gateway/") && strings.HasSuffix(name, ".go") && !strings.HasSuffix(name, "_test.go") && !strings.Contains(name, "/gen/") && !strings.HasPrefix(name, "gateway/internal/archcheck/") {
			found = append(found, releaseLayoutLiterals(name, source)...)
		}
	}
	sort.Strings(found)
	return found
}

// releaseLayoutSpellings are spellings of the release layout that only orchestrator/release may
// write. The host and the client learn where a part is from the release's manifest.
var releaseLayoutSpellings = []string{"release.json", ".venv"}

func releaseLayoutLiterals(name, source string) []string {
	file, err := parser.ParseFile(token.NewFileSet(), name, source, 0)
	if err != nil {
		return nil // the command check reports unparseable files.
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
		for _, s := range releaseLayoutSpellings {
			if strings.Contains(text, s) {
				found = append(found, fmt.Sprintf("%s: spells the release layout (%q); read release.json through orchestrator/release", name, s))
			}
		}
		return true
	})
	return found
}

func (c command) violations(files map[string]string) []string {
	var found []string
	var mains []string
	for name, source := range files {
		if !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		isMain := path.Dir(name) == c.directory
		if !isMain && !strings.HasPrefix(name, c.composition+"/") {
			continue
		}
		if isMain {
			mains = append(mains, name)
		}
		file, err := parser.ParseFile(token.NewFileSet(), name, source, parser.ImportsOnly)
		if err != nil {
			found = append(found, fmt.Sprintf("%s: cannot parse imports: %v", name, err))
			continue
		}
		var targets []string
		for _, spec := range file.Imports {
			target, err := strconv.Unquote(spec.Path.Value)
			if err != nil {
				found = append(found, fmt.Sprintf("%s: invalid import %s", name, spec.Path.Value))
				continue
			}
			targets = append(targets, target)
			if !isMain && !c.permits(target) {
				found = append(found, fmt.Sprintf("%s: forbidden import %s; %s only composes typed services", name, target, path.Base(c.composition)))
			}
		}
		if isMain {
			slices.Sort(targets)
			if !slices.Equal(targets, []string{module + c.composition, "os"}) {
				found = append(found, fmt.Sprintf("%s: command must import exactly os and %s", name, path.Base(c.composition)))
			}
		}
	}
	if len(mains) != 1 {
		found = append(found, fmt.Sprintf("%s: need exactly one non-test Go file, found %d; move composition into %s", c.directory, len(mains), path.Base(c.composition)))
	}
	return found
}

func (c command) permits(target string) bool {
	// Standard library import paths have no domain in their first component.
	if !strings.Contains(strings.Split(target, "/")[0], ".") && target != "C" {
		return true
	}
	return slices.Contains(c.allowed, target)
}
