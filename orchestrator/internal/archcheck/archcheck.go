// Package archcheck enforces the orchestrator's structural rules over its sources: the layer rules
// for imports, the isolation of this module from the rest of the repository, and the purity of the
// decision core.
//
// Violations is a pure function of the files it is given, so its tests need no checkout; the test
// in this package applies it to the real tree.
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
	repositoryPath = "github.com/uw-syfi/vibesys/"
	modulePath     = repositoryPath + "orchestrator/"
	moduleDir      = "orchestrator/"
	genDir         = moduleDir + "gen/"
	coreDir        = moduleDir + "internal/core/"
)

// rules lists, for each package directory under orchestrator/, the module packages it may import
// beyond itself. An entry ending in "/" allows the whole subtree. A package with Go files and no
// entry fails the check, so a new package states its layer in the change that adds it.
var rules = map[string][]string{
	"internal/clidaemon":               {"gen/vibesys/cli/"},
	"internal/clidaemon/clidaemontest": {"internal/clidaemon", "gen/vibesys/cli/"},
	"hosted":                           {"internal/client", "internal/core", "internal/daemon", "internal/mcp", "internal/production", "internal/search", "release", "gen/vibesys/orchestrator/"},
	"internal/production":              {"release", "hostlock", "internal/client", "internal/core", "internal/daemon", "internal/hub", "internal/jobs", "internal/search", "internal/clidaemon", "internal/localfs", "internal/store", "internal/wire", "gen/vibesys/orchestrator/"},
	"internal/core":                    {},
	"internal/search":                  {"internal/core", "internal/jobs", "gen/vibesys/cli/", "gen/vibesys/orchestrator/"},
	"internal/jobs":                    {"internal/core"},
	"internal/jobs/jobstest":           {"internal/core", "internal/jobs"},
	"internal/wire":                    {"internal/core", "gen/vibesys/orchestrator/"},
	"internal/archcheck":               {},
	"internal/store":                   {"internal/core", "internal/wire", "internal/localfs"},
	"internal/store/storetest":         {"internal/core", "internal/store", "internal/wire"},
	"internal/localfs":                 {},
	"gatewayroot":                      {"internal/localfs"},
	"release":                          {"gen/vibesys/orchestrator/"},
	"hostlock":                         {"gen/vibesys/orchestrator/"},
	"release/releasetest":              {"release"},
	"cmd/release-manifest":             {"release"},
	"clientcli":                        {"internal/client", "release", "gen/vibesys/orchestrator/"},
	"internal/hub":                     {"internal/core", "gen/vibesys/orchestrator/"},
	"internal/client":                  {"gen/vibesys/orchestrator/"},
	"internal/mcp":                     {"internal/core", "gen/vibesys/orchestrator/"},
	"internal/daemon":                  {"internal/search", "internal/core", "internal/hub", "internal/jobs", "internal/store", "internal/wire", "gen/vibesys/orchestrator/"},
	"internal/daemon/daemontest":       {"internal/core", "internal/daemon", "internal/hub", "internal/jobs/jobstest", "internal/jobs", "internal/store", "gen/vibesys/orchestrator/"},
	// The fake detector mints a Dir for any path, so it is a test fixture only (see testRules).
	"internal/localfs/localfstest": {"internal/localfs"},
}

// testRules lists, for each package directory, the packages only its test files may import in
// addition to rules. The core's simulation replays through the wire and draws jobs from the fake
// source; the core's own code reaches neither.
var testRules = map[string][]string{
	"internal/search": {"internal/clidaemon", "internal/clidaemon/clidaemontest", "internal/daemon/daemontest", "internal/wire", "gen/vibesys/orchestrator/"},
	// Upgrade fixtures compose the durable store and daemon using deterministic world Fakes.
	"internal/production": {"release/releasetest", "internal/clidaemon/clidaemontest", "internal/daemon/daemontest", "internal/localfs/localfstest"},
	"internal/clidaemon":  {"internal/clidaemon/clidaemontest"},
	"release":             {"release/releasetest"},
	// The store's tests run the contract suite, which depends on the store.
	// The store's tests also open stores on directories the fake detector vouches for, which
	// no non-test file may import.
	"internal/store": {"internal/store/storetest", "internal/localfs/localfstest"},
	// localfs's own tests run its checks against the fake.
	"internal/localfs": {"internal/localfs/localfstest"},
	"internal/client":  {"internal/core", "internal/daemon", "internal/daemon/daemontest", "internal/mcp"},
	"hosted":           {"release/releasetest", "gen/vibesys/orchestrator/"},
	// The verbs are tested against a whole daemon in memory, as the client library is.
	"clientcli":       {"release/releasetest", "internal/core", "internal/daemon", "internal/daemon/daemontest", "gen/vibesys/orchestrator/"},
	"internal/core":   {"internal/jobs/jobstest", "internal/wire", "gen/vibesys/orchestrator/"},
	"internal/wire":   {"internal/core", "internal/client"},
	"internal/daemon": {"internal/client", "internal/daemon/daemontest", "internal/store", "internal/jobs/jobstest"},
}

// pureImports is the standard library the core may use. Nothing here reaches a clock, a file, the
// network, a random source or a goroutine primitive, so a new need states itself in this list.
var pureImports = []string{"cmp", "fmt", "maps", "reflect", "slices", "strconv", "time"}

// impureTime matches the parts of package time that read the clock or wait. The core uses time only
// for values and arithmetic.
var impureTime = regexp.MustCompile(`\btime\.(Now|Since|Until|Sleep|After|AfterFunc|Tick|NewTimer|NewTicker)\b`)

// shellDirs are the packages of the daemon's shell and its clients that decide or relay and must
// run in a simulation: they read no clock, start no process, draw no random number and handle no
// signal. internal/production is where each of those happens, once.
var shellDirs = []string{"clientcli", "internal/search", "internal/clidaemon", "internal/daemon", "internal/hub", "internal/mcp", "internal/client"}

// realIO is the standard library a shell package may not import.
var realIO = []string{"os/exec", "os/signal", "crypto/rand", "math/rand", "syscall", "net/http"}

// confinement holds the shell packages' product code away from the real world.
func confinement(name, source string) []string {
	rel := strings.TrimPrefix(name, moduleDir)
	if strings.HasSuffix(name, "_test.go") || !slices.ContainsFunc(shellDirs, func(d string) bool {
		return strings.HasPrefix(rel, d+"/")
	}) {
		return nil
	}
	targets, err := imports(name, source)
	if err != nil {
		return nil // layering reports it.
	}
	var found []string
	for _, target := range targets {
		if slices.Contains(realIO, target) {
			found = append(found, fmt.Sprintf("%s: imports %s; real I/O belongs to internal/production", name, target))
		}
	}
	if match := impureTime.FindString(source); match != "" {
		found = append(found, fmt.Sprintf("%s: uses %s; time comes from the injected clock", name, match))
	}
	return found
}

// Violations checks the files, keyed by slash-separated path from the repository root, and returns
// one sorted line per violation.
func Violations(files map[string]string) []string {
	var found []string
	for name, source := range files {
		if strings.HasSuffix(name, ".go") && strings.HasPrefix(name, moduleDir) {
			found = append(found, layering(name, source)...)
			found = append(found, purity(name, source)...)
			found = append(found, confinement(name, source)...)
			found = append(found, capabilities(name, source)...)
			found = append(found, layoutLiterals(name, source)...)
			found = append(found, processScraping(name, source)...)
		}
	}
	sort.Strings(found)
	return found
}

func imports(name, source string) ([]string, error) {
	file, err := parser.ParseFile(token.NewFileSet(), name, source, parser.ImportsOnly)
	if err != nil {
		return nil, fmt.Errorf("%s: cannot parse imports: %w", name, err)
	}
	var out []string
	for _, spec := range file.Imports {
		target, err := strconv.Unquote(spec.Path.Value)
		if err != nil {
			return nil, fmt.Errorf("%s: bad import %s", name, spec.Path.Value)
		}
		out = append(out, target)
	}
	return out, nil
}

func layering(name, source string) []string {
	targets, err := imports(name, source)
	if err != nil {
		return []string{err.Error()}
	}
	dir := path.Dir(strings.TrimPrefix(name, moduleDir))
	var allowed []string
	if strings.HasPrefix(name, genDir) {
		allowed = []string{"gen/"}
	} else {
		listed, ok := rules[dir]
		if !ok {
			return []string{fmt.Sprintf("%s: package %s has no layer rule; add it to archcheck's rules", name, dir)}
		}
		allowed = append(slices.Clone(listed), dir)
		if strings.HasSuffix(name, "_test.go") {
			allowed = append(allowed, testRules[dir]...)
		}
	}
	var found []string
	for _, target := range targets {
		rest, inModule := strings.CutPrefix(target, modulePath)
		switch {
		case !inModule && strings.HasPrefix(target, repositoryPath) && target != "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1" && !(strings.HasSuffix(name, "/internal/clidaemon/client_integration_test.go") && target == "github.com/uw-syfi/vibesys/cli/daemontest"):
			found = append(found, fmt.Sprintf("%s: imports %s; this module stands apart from the rest of the repository", name, target))
		case inModule && !slices.ContainsFunc(allowed, func(rule string) bool { return permits(rule, rest) }):
			found = append(found, fmt.Sprintf("%s: imports %s, which its layer does not allow", name, rest))
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

// purity holds the core's product code to the standard library it may use and away from the clock.
func purity(name, source string) []string {
	if !strings.HasPrefix(name, coreDir) || strings.HasSuffix(name, "_test.go") {
		return nil
	}
	targets, err := imports(name, source)
	if err != nil {
		return nil // layering reports it.
	}
	var found []string
	for _, target := range targets {
		if !slices.Contains(pureImports, target) {
			found = append(found, fmt.Sprintf("%s: imports %s, and the core is pure", name, target))
		}
	}
	if match := impureTime.FindString(source); match != "" {
		found = append(found, fmt.Sprintf("%s: uses %s, and time reaches the core in its input", name, match))
	}
	return found
}
