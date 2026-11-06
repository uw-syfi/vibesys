package archcheck

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"sort"
	"strconv"
	"strings"
)

// allowance is one file's exact count of each boundary capability, with the reason the file is a
// boundary at all. The counts are exact on purpose: removing a use requires shrinking the entry,
// so a removed capability cannot make room for an unrelated new one, and a new use fails until the
// change names it here. A new process boundary should be a typed proto message instead, see
// docs/contributing/process-boundaries.md. The orchestrator module keeps the same table for its
// own sources; the two modules cannot share code through an internal package.
type allowance struct {
	reason string
	// reads and writes count os file operations, env counts os environment access, json counts
	// uses of encoding/json, exec counts command starts and args counts uses of os.Args.
	reads, writes, env, json, exec, args int
}

// boundaries holds the baseline, keyed by the file's path from the repository root.
var boundaries = map[string]allowance{
	"gateway/cmd/vibesys-client/main.go":              {reason: "a program's own argument vector", args: 1},
	"gateway/cmd/vibesys-host/main.go":                {reason: "a program's own argument vector", args: 1},
	"gateway/internal/clientmain/clientmain.go":       {reason: "the client finds the host program beside itself and takes its root from the environment; process-boundaries.md, release manifest and host control", env: 2},
	"gateway/internal/clientmain/hosts.go":            {reason: "the client takes its root from VIBESYS_HOME by gatewayroot.Rule; process-boundaries.md, host control", env: 1},
	"gateway/internal/connect/process.go":             {reason: "the client starts a host, on a clean environment, and keeps what it said on stderr for its own failure; process-boundaries.md, host control", exec: 1, reads: 4, writes: 4},
	"gateway/internal/hostcommand/runner.go":          {reason: "external:runs the host's own child commands", exec: 1},
	"gateway/internal/hostmain/hostmain.go":           {reason: "a host replaces the environment it inherited with its clean one, and a bridge starts a host on that; process-boundaries.md, host control", env: 5},
	"gateway/internal/hostmain/orchestrator.go":       {reason: "a host takes its root from VIBESYS_HOME by gatewayroot.Rule; process-boundaries.md, host control", env: 1},
	"gateway/internal/lifecycle/socket.go":            {reason: "the host's socket path, lock and stale socket handling, and the typed lock record a client reads while the lock is held; process-boundaries.md, host control", reads: 4, writes: 3},
	"gateway/internal/machinestats/host.go":           {reason: "external:/proc readings for MachineStats", reads: 1},
	"gateway/internal/runindex/index.go":              {reason: "the Python run store's disk input; deleted with the Python path", reads: 2},
	"gateway/internal/runlog/follow.go":               {reason: "the Python run store's run.log; deleted with the Python path", reads: 2, writes: 1},
	"gateway/internal/server/files.go":                {reason: "the Python run store's disk input; deleted with the Python path", reads: 2},
	"gateway/internal/server/run_address.go":          {reason: "the Python run store's disk input; deleted with the Python path", reads: 1},
	"gateway/internal/server/runs_source.go":          {reason: "the Python run store's disk input; deleted with the Python path", reads: 1},
	"gateway/internal/server/skeleton.go":             {reason: "the Python run store's disk input; deleted with the Python path", reads: 1},
	"gateway/internal/server/stream.go":               {reason: "the Python run store's disk input; deleted with the Python path", reads: 1},
	"gateway/internal/server/watch_backend_darwin.go": {reason: "the Python run store's disk input; deleted with the Python path", reads: 2},
}

const (
	osReads    = "|ReadFile|Open|OpenFile|ReadDir|Stat|Lstat|"
	osWrites   = "|WriteFile|Create|OpenFile|Rename|Remove|RemoveAll|Mkdir|MkdirAll|MkdirTemp|CreateTemp|Symlink|Link|Chmod|Truncate|"
	osEnv      = "|Getenv|LookupEnv|Environ|ExpandEnv|Setenv|Unsetenv|Clearenv|"
	execStarts = "|Command|CommandContext|"
)

type counts struct{ reads, writes, env, json, exec, args int }

func (c *counts) add(target, name string) {
	has := func(set string) bool { return strings.Contains(set, "|"+name+"|") }
	switch target {
	case "os":
		if has(osReads) {
			c.reads++
		}
		if has(osWrites) {
			c.writes++
		}
		if has(osEnv) {
			c.env++
		}
		if name == "Args" {
			c.args++
		}
	case "path/filepath":
		if strings.HasPrefix(name, "Walk") {
			c.reads++
		}
	case "encoding/json":
		c.json++
	case "os/exec":
		if has(execStarts) {
			c.exec++
		}
	}
}

// BoundaryViolations returns sorted diagnostics for files whose capability counts differ from the
// baseline. It is separate from Violations because it needs the whole tree: a partial tree would
// read as a shrunk baseline. Files use repository-relative, slash-separated paths.
func BoundaryViolations(files map[string]string) []string {
	var found []string
	for name, source := range files {
		found = append(found, fileBoundaryViolations(name, source)...)
	}
	sort.Strings(found)
	return found
}

// fileBoundaryViolations compares a file's capability counts with its baseline. Generated code,
// tests and the end-to-end world are not product boundaries.
func fileBoundaryViolations(name, source string) []string {
	if !strings.HasPrefix(name, "gateway/") || strings.HasPrefix(name, "gateway/gen/") || strings.HasPrefix(name, "gateway/e2e/") ||
		!strings.HasSuffix(name, ".go") || strings.HasSuffix(name, "_test.go") {
		return nil
	}
	file, err := parser.ParseFile(token.NewFileSet(), name, source, 0)
	if err != nil {
		return nil
	}
	aliases := map[string]string{}
	var found []string
	report := func(rule, detail string) { found = append(found, fmt.Sprintf("%s: %s: %s", name, rule, detail)) }
	for _, imp := range file.Imports {
		target, _ := strconv.Unquote(imp.Path.Value)
		alias := target[strings.LastIndex(target, "/")+1:]
		if imp.Name != nil {
			alias = imp.Name.Name
		}
		aliases[alias] = target
		if alias == "." && (target == "os" || target == "path/filepath" || target == "os/exec" || target == "encoding/json") {
			report("capability-import", "use a named import so capabilities are attributable")
		}
	}
	var got counts
	ast.Inspect(file, func(n ast.Node) bool {
		if sel, ok := n.(*ast.SelectorExpr); ok {
			if id, ok := sel.X.(*ast.Ident); ok {
				got.add(aliases[id.Name], sel.Sel.Name)
			}
		}
		return true
	})
	want := boundaries[name]
	for _, c := range []struct {
		rule, what, fix string
		got, want       int
	}{
		{"fs-reads", "file reads", "remove new reads", got.reads, want.reads},
		{"fs-writes", "file writes", "remove new writes", got.writes, want.writes},
		{"env-reads", "environment accesses", "pass the value in through a typed message", got.env, want.env},
		{"json-use", "uses of encoding/json", "send a proto message instead of hand JSON", got.json, want.json},
		{"exec-calls", "command starts", "remove new starts", got.exec, want.exec},
		{"args-use", "uses of os.Args", "pass the value in through a typed message", got.args, want.args},
	} {
		if c.got != c.want {
			report(c.rule, fmt.Sprintf("%d %s, baseline %d in gateway/internal/archcheck/boundaries.go; %s (docs/contributing/process-boundaries.md), or shrink the justified baseline when a use was removed", c.got, c.what, c.want, c.fix))
		}
	}
	return found
}

// StaleBaselines names the baseline entries that no longer hold a boundary: the file is gone, or
// every count is zero. exists reports whether a repository-relative path is a source file.
func StaleBaselines(exists func(name string) bool) []string {
	var stale []string
	for name, a := range boundaries {
		if !exists(name) {
			stale = append(stale, fmt.Sprintf("%s: baseline entry for a file that is gone; delete the entry in gateway/internal/archcheck/boundaries.go", name))
		} else if a.reads+a.writes+a.env+a.json+a.exec+a.args == 0 {
			stale = append(stale, fmt.Sprintf("%s: baseline entry with every count zero; delete the entry in gateway/internal/archcheck/boundaries.go", name))
		}
	}
	sort.Strings(stale)
	return stale
}
