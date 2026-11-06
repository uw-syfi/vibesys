package archcheck

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"path"
	"strconv"
	"strings"
)

type allowance struct {
	count  int
	reason string
}

// Exact counts ratchet existing reads. Removing a read requires shrinking its
// entry, so a removed capability cannot make room for an unrelated new one.
var fsReads = map[string]allowance{
	"internal/store/lock_unix.go":          {1, "the store's own advisory lock"},
	"internal/production/holder.go":        {1, "the orchestrator's own holder record"},
	"internal/production/plugin.go":        {1, "the configured plugin manifest"},
	"internal/production/production.go":    {1, "the orchestrator's own boot identity"},
	"internal/production/sidecars.go":      {9, "sidecar source discovery and legacy pid, pgid and log reads; deferred sidecar-link migration F1"},
	"internal/production/agentcli.go":      {1, "checks that the chosen vibesys-cli is an executable file"},
	"internal/production/startchecks.go":   {1, "checks a start's repository and task directory exist before the run is decided"},
	"internal/production/cli_transport.go": {1, "the callee-owned discovery contract requires inspecting socket directory metadata"},
}
var execFiles = map[string]string{
	"internal/production/cli_ensure.go":  "the callee owns shared daemon startup through daemon ensure",
	"internal/production/sidecars.go":    "sidecar lifetime and git worktree creation",
	"internal/production/startchecks.go": "start checks read the repository's history with git",
}

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
		if alias == "." && (target == "os" || target == "path/filepath" || target == "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1") {
			report("capability-import", "use a named import so capabilities are attributable")
		}
	}
	reads := 0
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
		if (target == "os" && strings.Contains("|ReadFile|Open|OpenFile|ReadDir|Stat|Lstat|", "|"+sel.Sel.Name+"|")) || (target == "path/filepath" && strings.HasPrefix(sel.Sel.Name, "Walk")) {
			reads++
		}
		return true
	})
	if expected, ok := fsReads[rel]; reads != expected.count || (reads > 0 && !ok) {
		report("fs-reads", fmt.Sprintf("%d file-read capabilities, baseline %d; remove new reads or shrink the justified baseline", reads, expected.count))
	}
	return found
}
