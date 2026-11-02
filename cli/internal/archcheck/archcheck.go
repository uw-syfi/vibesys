// Package archcheck enforces vibesys-cli's structural rules over its sources: the layer rules
// for imports, the rule that knowledge of the storage layout stays inside internal/store/filestore,
// and the rules that keep the daemon simulable and its decoders fuzzed: the real clock is reached
// only through internal/clock, goroutines in the daemon's packages start through owned.Group, and
// every decoder in internal/wire has a fuzz target.
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
	// The command line reads flags and prints. It reaches the daemon through the client, resolves
	// revisions through gitrepo, and in the binary's wiring through production. It never names the
	// domain: it speaks the contract's messages, and wire translates.
	"cmd/vibesys-cli": {"gen/", "internal/wire", "internal/client", "internal/gitrepo", "internal/production", "internal/upgrade/"},

	// The domain and what it needs from the machine.
	"internal/core":                {},
	"internal/clock":               {},
	"internal/changed":             {},
	"internal/owned":               {},
	"internal/lifecycle":           {"internal/core"},
	"internal/backend":             {"internal/core"},
	"internal/environment":         {"internal/core"},
	"internal/environment/fakeenv": {"internal/core", "internal/environment"},
	"internal/gitrepo":             {"internal/core"},
	"internal/runtimedir":          {"internal/core"},

	// Storage.
	"internal/store":                {"internal/core"},
	"internal/store/ledger":         {"internal/core", "internal/store"},
	"internal/store/storetest":      {"internal/core", "internal/store"},
	"internal/store/memstore":       {"internal/core", "internal/store", "internal/store/ledger"},
	"internal/store/filestore":      {"internal/core", "internal/store", "internal/store/ledger", "internal/store/filestore/fsys"},
	"internal/store/filestore/fsys": {},

	// The translation between the domain and the contract.
	"internal/wire": {"internal/core", "internal/lifecycle", "gen/"},

	// Supervisors: the domain, its channel to the daemon, and the process that serves it.
	"internal/supervisor":         {"internal/changed", "internal/core", "internal/environment", "internal/lifecycle", "internal/owned"},
	"internal/supervisor/suptest": {"internal/core", "internal/lifecycle", "internal/owned", "internal/supervisor"},
	"internal/supervisor/rpc":     {"gen/", "internal/core", "internal/lifecycle", "internal/supervisor", "internal/wire"},
	"internal/supervisor/host": {
		"gen/", "internal/clock", "internal/core", "internal/environment", "internal/lifecycle", "internal/owned",
		"internal/runtimedir", "internal/supervisor", "internal/supervisor/rpc", "internal/wire",
	},

	// The daemon: its job flows, the process that serves them, and the client that calls it. The
	// client speaks the contract's messages only, so it does not import the domain.
	"internal/app": {
		"internal/changed", "internal/clock", "internal/core", "internal/environment", "internal/lifecycle", "internal/owned",
		"internal/store", "internal/supervisor",
	},
	"internal/app/filestores": {"internal/app", "internal/store", "internal/store/filestore"},
	"internal/app/memstores":  {"internal/app", "internal/store", "internal/store/memstore"},
	"internal/daemon": {
		"gen/", "internal/app", "internal/clock", "internal/core", "internal/environment", "internal/owned",
		"internal/runtimedir", "internal/supervisor", "internal/wire",
	},
	"internal/daemon/daemontest": {
		"internal/app", "internal/app/memstores", "internal/clock", "internal/core", "internal/daemon",
		"internal/environment/fakeenv", "internal/owned", "internal/runtimedir", "internal/supervisor",
	},
	"internal/client": {"gen/", "internal/clock", "internal/runtimedir", "internal/wire"},

	// The simulation of the daemon has tests and no product code.
	"internal/sim": {},

	// The wiring of the shipped binary.
	"internal/production": {
		"internal/app", "internal/app/filestores", "internal/client", "internal/clock", "internal/core",
		"internal/daemon", "internal/environment", "internal/runtimedir", "internal/store/filestore",
		"internal/supervisor", "internal/supervisor/host",
	},

	// Replacing the running binary with a newer release.
	"internal/upgrade":             {"internal/upgrade/upgradetest"},
	"internal/upgrade/upgradetest": {"internal/upgrade"},

	"internal/archcheck": {},
}

// testRules lists, for each package directory under cli/, the packages only its test files may
// import in addition to rules: the contract suite an implementation runs, a fake or another
// implementation to compare it with, and the packages a test composes a daemon from. Product code
// reaches none of them, so a fake never ships.
var testRules = map[string][]string{
	"internal/store/ledger":    {"internal/clock"},
	"internal/store/memstore":  {"internal/store/storetest"},
	"internal/store/filestore": {"internal/store/storetest", "internal/store/memstore"},
	"internal/supervisor":      {"internal/environment/fakeenv", "internal/supervisor/suptest"},
	"internal/supervisor/rpc":  {"internal/environment/fakeenv", "internal/supervisor/suptest"},
	"internal/supervisor/host": {"internal/environment/fakeenv", "internal/supervisor/suptest"},
	"internal/app":             {"internal/app/memstores", "internal/environment/fakeenv"},
	"internal/app/filestores":  {"internal/core"},
	"internal/daemon":          {"internal/app/memstores", "internal/environment/fakeenv"},
	"internal/client": {
		"internal/app", "internal/app/memstores", "internal/core", "internal/daemon", "internal/daemon/daemontest",
		"internal/environment/fakeenv", "internal/supervisor",
	},
	// The whole daemon, over Fakes, in one process.
	"internal/sim": {
		"gen/", "internal/app", "internal/app/memstores", "internal/client", "internal/clock", "internal/core",
		"internal/daemon", "internal/environment/fakeenv", "internal/lifecycle", "internal/owned",
		"internal/runtimedir", "internal/store", "internal/supervisor",
	},
	"cmd/vibesys-cli": {
		"internal/app", "internal/app/filestores", "internal/clock", "internal/core", "internal/daemon",
		"internal/daemon/daemontest", "internal/environment/fakeenv", "internal/runtimedir",
		"internal/store/filestore", "internal/supervisor/host",
	},
}

// layoutNames are spellings that only the on-disk layout may use: the state directory, the event
// file, the per-user index location, and layout directory names joined into a path.
var layoutNames = []*regexp.Regexp{
	regexp.MustCompile(`\.vibesys-state`),
	regexp.MustCompile(`events\.jsonl`),
	regexp.MustCompile(`\.local/state`),
	regexp.MustCompile(`XDG_STATE_HOME`),
	regexp.MustCompile(`"(jobs|plans|promotions|results|runs|logs)/`),
	regexp.MustCompile(`(?:filepath|path)\.Join\([^)]*"(jobs|plans|promotions|results|runs|logs)"`),
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
			found = append(found, effects(name, source)...)
		case strings.HasSuffix(name, ".proto") && strings.HasPrefix(name, "proto/vibesys/cli/"):
			found = append(found, layout(name, source)...)
		}
	}
	found = append(found, decoderViolations(files)...)
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
	allowed = append(slices.Clone(allowed), dir)
	if strings.HasSuffix(name, "_test.go") {
		allowed = append(allowed, testRules[dir]...)
	}
	return disallowed(name, imports, allowed)
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

// Rules returns a copy of the layer table: for each package directory, the packages its product
// code may import.
func Rules() map[string][]string {
	out := make(map[string][]string, len(rules))
	for dir, allowed := range rules {
		out[dir] = slices.Clone(allowed)
	}
	return out
}
