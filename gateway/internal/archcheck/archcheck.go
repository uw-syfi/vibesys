// Package archcheck enforces the host's composition boundary over supplied Go sources.
package archcheck

import (
	"fmt"
	"go/parser"
	"go/token"
	"path"
	"slices"
	"sort"
	"strconv"
	"strings"
)

// CommandDirectory is the single command path changed when the host is renamed.
const CommandDirectory = "gateway/cmd/vibesys-host"

const module = "github.com/uw-syfi/vibesys/"

// Violations returns sorted diagnostics for the command shape and hostmain imports.
// Files use repository-relative, slash-separated paths; tests are unrestricted.
func Violations(files map[string]string) []string {
	var found []string
	var commands []string
	for name, source := range files {
		if !strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
			continue
		}
		command := path.Dir(name) == CommandDirectory
		if !command && !strings.HasPrefix(name, "gateway/internal/hostmain/") {
			continue
		}
		if command {
			commands = append(commands, name)
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
			if !command && !allowed(target) {
				found = append(found, fmt.Sprintf("%s: forbidden import %s; hostmain only composes typed services", name, target))
			}
		}
		if command {
			slices.Sort(targets)
			if !slices.Equal(targets, []string{module + "gateway/internal/hostmain", "os"}) {
				found = append(found, fmt.Sprintf("%s: command must import exactly os and hostmain", name))
			}
		}
	}
	if len(commands) != 1 {
		found = append(found, fmt.Sprintf("%s: need exactly one non-test Go file, found %d; move composition into hostmain", CommandDirectory, len(commands)))
	}
	sort.Strings(found)
	return found
}

func allowed(target string) bool {
	// Standard library import paths have no domain in their first component.
	if !strings.Contains(strings.Split(target, "/")[0], ".") && target != "C" {
		return true
	}
	return slices.Contains([]string{
		module + "gateway/internal/connect", module + "gateway/internal/lifecycle",
		module + "gateway/internal/pipe", module + "gateway/internal/server",
		module + "gateway/schemahash", module + "orchestrator/hosted",
	}, target)
}
