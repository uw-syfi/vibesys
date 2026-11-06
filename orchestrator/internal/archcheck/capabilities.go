package archcheck

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"path"
	"sort"
	"strconv"
	"strings"
)

// allowance is one file's exact count of each boundary capability, with the reason the file is a
// boundary at all. The counts are exact on purpose: removing a use requires shrinking the entry,
// so a removed capability cannot make room for an unrelated new one, and a new use fails until the
// change names it here. A new process boundary should be a typed proto message instead, see
// docs/contributing/process-boundaries.md.
type allowance struct {
	reason string
	// reads and writes count os file operations, env counts os environment access, json counts
	// uses of encoding/json, exec counts command starts and args counts uses of os.Args.
	reads, writes, env, json, exec, args int
}

// boundaries holds the baseline, keyed by the file's path under orchestrator/.
var boundaries = map[string]allowance{
	"cmd/release-manifest/main.go":               {reason: "the build-time tool takes its output path from the command line and writes the release manifest; process-boundaries.md, release manifest", args: 1, writes: 1},
	"privatesocket/privatesocket.go":             {reason: "binds a private socket in a directory of its own and renames it into place; process-boundaries.md, host control", writes: 6},
	"release/releasetest/releasetest.go":         {reason: "test support that writes release fixtures; process-boundaries.md, release manifest", writes: 2},
	"clientcli/start.go":                         {reason: "run get --json prints the protojson of a run snapshot", json: 1},
	"hostconfig/hostconfig.go":                   {reason: "the host's configuration file in its root, a HostConfig message; process-boundaries.md, host control", reads: 1, writes: 5},
	"hosted/command.go":                          {reason: "the tool server reads its identity from the environment; process-boundaries.md, environment codec", env: 1},
	"internal/clidaemon/clidaemontest/corpus.go": {reason: "the conformance corpus is JSON beside the CLI's own", json: 2},
	"internal/mcp/mcp.go":                        {reason: "external:MCP framing is JSON-RPC; tool schemas move to descriptors, see process-boundaries.md, tool schemas", json: 17},
	"internal/mcp/tools.go":                      {reason: "hand tool schemas beside the protos; process-boundaries.md, tool schemas", json: 7},
	"internal/production/agentcli.go":            {reason: "checks that the chosen vibesys-cli is an executable file", reads: 1},
	"internal/production/cli_ensure.go":          {reason: "the callee owns shared daemon startup through daemon ensure; process-boundaries.md, job tool endpoint", env: 1, exec: 1},
	"internal/production/cli_transport.go":       {reason: "the callee-owned discovery contract requires inspecting socket directory metadata", reads: 1},
	"internal/production/holder.go":              {reason: "the orchestrator's own holder record; process-boundaries.md, host control", reads: 1, writes: 2},
	"internal/production/holders.go":             {reason: "the holder's log, its pid and process groups until the session lock replaces them, and git worktree creation; process-boundaries.md, session lock", reads: 6, writes: 5, exec: 2},
	"internal/production/production.go":          {reason: "the orchestrator's own boot identity and state directory", reads: 1, writes: 6},
	"internal/production/sidecars.go":            {reason: "where the holder is installed, and its directory; process-boundaries.md, host control", reads: 2, writes: 1},
	"internal/production/startchecks.go":         {reason: "checks a start's repository and task directory exist, and reads the repository with git", reads: 1, exec: 1},
	"internal/store/lock_unix.go":                {reason: "the store's own advisory lock", reads: 1, writes: 1},
	"internal/store/sqlite.go":                   {reason: "the store's own database file", writes: 1},
	"internal/store/storetest/contract.go":       {reason: "the contract suite reads its depth from the environment", env: 2},
}

// execFiles may import os/exec; their start counts are in boundaries.
var execFiles = map[string]string{
	"internal/production/cli_ensure.go":  "the callee owns shared daemon startup through daemon ensure",
	"internal/production/holders.go":     "the lifetime of a session's process and git worktree creation",
	"internal/production/startchecks.go": "start checks read the repository's history with git",
}

const (
	osReads    = "|ReadFile|Open|OpenFile|ReadDir|Stat|Lstat|"
	osWrites   = "|WriteFile|Create|OpenFile|Rename|Remove|RemoveAll|Mkdir|MkdirAll|MkdirTemp|CreateTemp|Symlink|Link|Chmod|Truncate|"
	osEnv      = "|Getenv|LookupEnv|Environ|ExpandEnv|Setenv|Unsetenv|Clearenv|"
	execStarts = "|Command|CommandContext|"
)

// counts is what a file does at the process boundary, in the units of allowance.
type counts struct{ reads, writes, env, json, exec, args int }

func capabilities(name, source string) []string {
	if strings.HasPrefix(name, genDir) || strings.HasSuffix(name, "_test.go") {
		return nil
	}
	file, err := parser.ParseFile(token.NewFileSet(), name, source, 0)
	if err != nil {
		return nil
	}
	aliases := map[string]string{}
	rel := strings.TrimPrefix(name, moduleDir)
	var found []string
	report := func(rule, detail string) { found = append(found, fmt.Sprintf("%s: %s: %s", name, rule, detail)) }
	for _, imp := range file.Imports {
		target, _ := strconv.Unquote(imp.Path.Value)
		alias := target[strings.LastIndex(target, "/")+1:]
		if imp.Name != nil {
			alias = imp.Name.Name
		}
		aliases[alias] = target
		if target == "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1" && !strings.HasPrefix(rel, "internal/clidaemon/") && !strings.HasPrefix(rel, "internal/search/") && !strings.HasPrefix(rel, "clientcli/") {
			report("cli-contract-importers", "CLI contract belongs to clidaemon, search and the client verbs that show a search view")
		}
		if target == "os/exec" && execFiles[rel] == "" {
			report("exec-confined", "process execution belongs to listed production files")
		}
		if alias == "." && (target == "os" || target == "path/filepath" || target == "encoding/json" || target == "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1") {
			report("capability-import", "use a named import so capabilities are attributable")
		}
	}
	var got counts
	ast.Inspect(file, func(n ast.Node) bool {
		sel, ok := n.(*ast.SelectorExpr)
		if !ok {
			return true
		}
		id, ok := sel.X.(*ast.Ident)
		if !ok {
			return true
		}
		target := aliases[id.Name]
		if target == "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1" && (sel.Sel.Name == "NewDaemonServiceClient" || sel.Sel.Name == "DaemonServiceClient" || sel.Sel.Name == "NewSupervisorServiceClient" || sel.Sel.Name == "SupervisorServiceClient") && path.Dir(rel) != "internal/clidaemon" {
			report("cli-stub", "construct CLI stubs only in clidaemon")
		}
		got.add(target, sel.Sel.Name)
		return true
	})
	want := boundaries[rel]
	for _, c := range []struct {
		rule      string
		got, want int
		what      string
		fix       string
	}{
		{"fs-reads", got.reads, want.reads, "file reads", "remove new reads"},
		{"fs-writes", got.writes, want.writes, "file writes", "remove new writes"},
		{"env-reads", got.env, want.env, "environment accesses", "pass the value in through a typed message"},
		{"json-use", got.json, want.json, "uses of encoding/json", "send a proto message instead of hand JSON"},
		{"exec-calls", got.exec, want.exec, "command starts", "remove new starts"},
		{"args-use", got.args, want.args, "uses of os.Args", "pass the value in through a typed message"},
	} {
		if c.got != c.want {
			report(c.rule, fmt.Sprintf("%d %s, baseline %d in internal/archcheck/capabilities.go; %s (docs/contributing/process-boundaries.md), or shrink the justified baseline when a use was removed", c.got, c.what, c.want, c.fix))
		}
	}
	return found
}

// StaleBaselines names the baseline entries that no longer hold a boundary: the file is gone, or
// every count is zero. exists reports whether a path under orchestrator/ is a source file.
func StaleBaselines(exists func(rel string) bool) []string {
	var stale []string
	for rel, a := range boundaries {
		if !exists(rel) {
			stale = append(stale, fmt.Sprintf("%s%s: baseline entry for a file that is gone; delete the entry in internal/archcheck/capabilities.go", moduleDir, rel))
		} else if a.reads+a.writes+a.env+a.json+a.exec+a.args == 0 {
			stale = append(stale, fmt.Sprintf("%s%s: baseline entry with every count zero; delete the entry in internal/archcheck/capabilities.go", moduleDir, rel))
		}
	}
	sort.Strings(stale)
	return stale
}

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
