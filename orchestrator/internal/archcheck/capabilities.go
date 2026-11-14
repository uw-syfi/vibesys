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
	"privatesocket/placement.go":                 {reason: "makes and checks the private directory that holds a socket too long for its own place; process-boundaries.md, host control", reads: 1, writes: 1},
	"privatesocket/privatesocket.go":             {reason: "binds a private socket in a directory of its own and renames it into place; process-boundaries.md, host control", writes: 6},
	"cmd/release-manifest/examples.go":           {reason: "the build-time tool makes a working folder for the CLI's daemon, stages the examples and writes the catalogue; process-boundaries.md, release manifest", writes: 4},
	"release/releasetest/releasetest.go":         {reason: "test support that writes release fixtures; process-boundaries.md, release manifest", writes: 3},
	"release/assemble/os.go":                     {reason: "the release builder writes the example trees into the stage, runs the CLI's card reader and fetches the pinned files of external examples with git; process-boundaries.md, release manifest", writes: 3, exec: 2},
	"clientcli/start.go":                         {reason: "run get --json prints the protojson of a run snapshot", json: 1},
	"gatewayroot/gatewayroot.go":                 {reason: "says before a host starts whether its state directory can be made, as the host itself would find; process-boundaries.md, host control", reads: 1},
	"hostconfig/hostconfig.go":                   {reason: "the host's configuration file in its root, a HostConfig message, and whether what it names is there; process-boundaries.md, host control", reads: 3, writes: 5},
	"hostconfig/hostsettings.go":                 {reason: "the machine's model policy, ~/.config/vibesys/host.toml, which the Python engine reads for the same key; tests/corpus/host_settings holds the cases both readers agree on", reads: 1},
	"hostlock/hostlock.go":                       {reason: "the state directory's lock and the HostLock record its holder writes, read to name the holder, and only while the lock is held; process-boundaries.md, host control", reads: 2},
	"hosted/command.go":                          {reason: "the tool server reads its credentials from the environment, through the environment codec (cli/envcodec); process-boundaries.md, environment codec", env: 1},
	"internal/clidaemon/clidaemontest/corpus.go": {reason: "the conformance corpus is JSON beside the CLI's own", json: 2},
	"internal/mcp/args.go":                       {reason: "external:the arguments of a tool call are JSON, read against the input message's descriptor; process-boundaries.md, tool schemas", json: 3},
	"internal/mcp/mcp.go":                        {reason: "external:MCP framing is JSON-RPC; process-boundaries.md, tool schemas", json: 14},
	"internal/mcp/tools.go":                      {reason: "external:a tool call's arguments arrive as raw JSON; process-boundaries.md, tool schemas", json: 2},
	"internal/production/agentcli.go":            {reason: "checks that the chosen vibesys-cli is an executable file", reads: 1},
	"internal/production/checkouts.go":           {reason: "removes the clean worktrees agents made when their run is over, keeps the others, and holds the commits they made under a ref of the run; process-boundaries.md, session lock", reads: 2, exec: 6},
	"internal/production/cli_ensure.go":          {reason: "the callee owns shared daemon startup through daemon ensure; process-boundaries.md, job tool endpoint", env: 1, exec: 1},
	"internal/production/holder.go":              {reason: "the orchestrator's own holder record; process-boundaries.md, host control", reads: 1, writes: 2},
	"internal/production/holders.go":             {reason: "the holder's log, a group leader's start time from ps and git worktree creation; process-boundaries.md, process identity", reads: 2, writes: 2, exec: 3},
	"internal/production/production.go":          {reason: "the orchestrator's own boot identity and state directory", reads: 1, writes: 6},
	"internal/production/sidecars.go":            {reason: "where the holder is installed, its directory, and the listing of an agent's session directories; process-boundaries.md, host control", reads: 3, writes: 1},
	"internal/production/provision.go":           {reason: "makes a new repository in a staging directory beside its place, commits it with git and renames it into the host's repositories directory; its steps are one interface so tests break each in turn", reads: 3, writes: 6, exec: 1},
	"internal/production/sessionprocesses.go":    {reason: "the lock a session's process holds and the SessionProcesses record it keeps beside it, which the host reads only to end what a gone process left; process-boundaries.md, process identity", reads: 2, writes: 2},
	"internal/production/startchecks.go":         {reason: "checks a start's repository and task directory exist, reads the repository with git, and lists the task folders and the children of a folder an operator is typing a path into", reads: 2, exec: 1},
	"internal/store/lock_unix.go":                {reason: "the store's own advisory lock", reads: 1, writes: 1},
	"internal/store/sqlite.go":                   {reason: "the store's own database file", writes: 1},
	"internal/store/storetest/contract.go":       {reason: "the contract suite reads its depth from the environment", env: 2},
}

// execFiles may import os/exec; their start counts are in boundaries.
var execFiles = map[string]string{
	"release/assemble/os.go":             "the builder runs the CLI that reads task files, and git for the pinned files of external examples",
	"internal/production/checkouts.go":   "removing the git worktrees agents made, as holders.go made them, and keeping their commits under a ref",
	"internal/production/cli_ensure.go":  "the callee owns shared daemon startup through daemon ensure",
	"internal/production/holders.go":     "the lifetime of a session's process, ps for a group leader's start time, and git worktree creation",
	"internal/production/provision.go":   "git init, commit and clone of a new repository",
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
		if target == "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1" && !strings.HasPrefix(rel, "internal/clidaemon/") && !strings.HasPrefix(rel, "internal/search/") && !strings.HasPrefix(rel, "clientcli/") && !strings.HasPrefix(rel, "release/") && rel != "internal/production/agentenv.go" && rel != "internal/production/cli_ensure.go" && rel != "internal/wire/run_facts.go" && rel != "internal/wire/setup_facts.go" && rel != "internal/fit/fit.go" && rel != "internal/production/taskreader.go" && rel != "internal/daemon/run_search.go" {
			report("cli-contract-importers", "CLI contract belongs to clidaemon, search, the client verbs that show a search view, the agent environment, the job tool's answer, the task reader, the wire mapping of a task card and of the setup facts the fit of a task card's need, the run search that reads a run's plan and workstream facts and the release builder's catalogue of cards, whose messages are the CLI's")
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
