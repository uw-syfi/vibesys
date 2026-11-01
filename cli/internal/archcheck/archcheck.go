// Package archcheck enforces vibesys-cli's two structural rules over its sources: the layer rules
// for imports, and the rule that knowledge of the storage layout stays inside
// internal/store/filestore.
//
// Violations is a pure function of the files it is given, so its tests need no checkout; the
// test in this package applies it to the real tree.
package archcheck

import (
	"fmt"
	"go/parser"
	"go/token"
	"path"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
)

const (
	modulePath  = "github.com/uw-syfi/vibesys/cli/"
	filestoreAt = "cli/internal/store/filestore/"
	genAt       = "cli/gen/"
)

// rules lists, for each package directory under cli/, the module packages it may import beyond
// itself. An entry ending in "/" allows the whole subtree. A package with Go files and no entry
// fails the check, so a new package states its layer in the change that adds it.
var rules = map[string][]string{
	"cmd/vibesys-cli":          {"gen/", "internal/wire"},
	"internal/core":            {},
	"internal/store":           {"internal/core"},
	"internal/store/filestore": {"internal/core", "internal/store"},
	"internal/store/memstore":  {"internal/core", "internal/store"},
	"internal/environment":     {"internal/core"},
	"internal/backend":         {"internal/core"},
	"internal/wire":            {"internal/core", "gen/"},
	"internal/archcheck":       {},
}

// layoutNames are spellings that only the on-disk layout may use: the state directory, the event
// file, the per-user index location, and layout directory names joined into a path.
var layoutNames = []*regexp.Regexp{
	regexp.MustCompile(`\.vibesys-state`),
	regexp.MustCompile(`events\.jsonl`),
	regexp.MustCompile(`\.local/state`),
	regexp.MustCompile(`XDG_STATE_HOME`),
	regexp.MustCompile(`"(results|runs|logs)/`),
	regexp.MustCompile(`(?:filepath|path)\.Join\([^)]*"(results|runs|logs)"`),
}

// Violations checks the files, keyed by slash-separated path from the repository root, and
// returns one sorted line per violation. Go files under cli/ are checked for layering and for
// layout names; proto files under proto/vibesys/cli/ only for layout names.
func Violations(files map[string]string) []string {
	var found []string
	for name, source := range files {
		switch {
		case strings.HasSuffix(name, ".go") && strings.HasPrefix(name, "cli/"):
			found = append(found, layering(name, source)...)
			found = append(found, layout(name, source)...)
		case strings.HasSuffix(name, ".proto") && strings.HasPrefix(name, "proto/vibesys/cli/"):
			found = append(found, layout(name, source)...)
		}
	}
	sort.Strings(found)
	return found
}

func layering(name, source string) []string {
	dir := path.Dir(strings.TrimPrefix(name, "cli/"))
	imports, err := moduleImports(name, source)
	if err != nil {
		return []string{err.Error()}
	}
	if strings.HasPrefix(name, genAt) {
		return disallowed(name, imports, []string{"gen/"})
	}
	allowed, ok := rules[dir]
	if !ok {
		return []string{fmt.Sprintf("%s: package %s has no layer rule; add it to archcheck's rules", name, dir)}
	}
	return disallowed(name, imports, append(slices.Clone(allowed), dir))
}

func moduleImports(name, source string) ([]string, error) {
	file, err := parser.ParseFile(token.NewFileSet(), name, source, parser.ImportsOnly)
	if err != nil {
		return nil, fmt.Errorf("%s: cannot parse imports: %w", name, err)
	}
	var imports []string
	for _, spec := range file.Imports {
		target, err := strconv.Unquote(spec.Path.Value)
		if err != nil {
			return nil, fmt.Errorf("%s: bad import %s", name, spec.Path.Value)
		}
		if rest, ok := strings.CutPrefix(target, modulePath); ok {
			imports = append(imports, rest)
		}
	}
	return imports, nil
}

func disallowed(name string, imports, allowed []string) []string {
	var found []string
	for _, target := range imports {
		if !slices.ContainsFunc(allowed, func(rule string) bool { return permits(rule, target) }) {
			found = append(found, fmt.Sprintf("%s: imports %s, which its layer does not allow", name, target))
		}
	}
	return found
}

func permits(rule, target string) bool {
	if strings.HasSuffix(rule, "/") {
		return strings.HasPrefix(target+"/", rule)
	}
	return target == rule
}

func layout(name, source string) []string {
	if strings.HasPrefix(name, filestoreAt) || strings.HasPrefix(name, "cli/internal/archcheck/") {
		return nil
	}
	var found []string
	for _, pattern := range layoutNames {
		if match := pattern.FindString(source); match != "" {
			found = append(found, fmt.Sprintf("%s: names the storage layout (%q); only internal/store/filestore may", name, match))
		}
	}
	return found
}
