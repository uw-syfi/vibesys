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
	// The public test server other modules start the CLI daemon with, over Fakes.
	"daemontest": {
		"gen/", "internal/app", "internal/app/memstores", "internal/clock", "internal/core", "internal/daemon",
		"internal/environment/fakeenv", "internal/gitrepo", "internal/supervisor",
	},
	"cmd/vibesys-cli": {"gen/", "internal/wire", "internal/client", "internal/gitrepo", "internal/production", "internal/upgrade/"},

	// The domain and what it needs from the machine.
	"internal/core":                {},
	"internal/clock":               {},
	"internal/changed":             {},
	"internal/owned":               {},
	"internal/lifecycle":           {"internal/core", "internal/flow", "internal/schedule"},
	"internal/backend":             {"internal/core"},
	"internal/backend/cpu":         {"internal/backend", "internal/clock", "internal/core"},
	"internal/backend/backendtest": {"internal/backend"},
	"internal/backend/nvidia":      {"internal/clock", "internal/core", "internal/remote"},
	"internal/backend/amd":         {"internal/clock", "internal/core", "internal/remote"},
	// Reaching a machine to run programs on: this one, or another over ssh and rsync.
	"internal/remote": {"internal/backend/cpu", "internal/clock", "internal/core"},
	// The operator's settings of the machine, which never come from a task: the outer structure of
	// the file, decoded into the contract's message. What each placement or resource kind's section
	// means is that kind's package's business, which only production names.
	"internal/operator": {"gen/", "internal/core", "internal/tomlproto"},
	// A TOML file decoded into a contract message by its field names, refusing unknown keys and
	// keys newer than the schema version the file declares.
	"internal/tomlproto": {"gen/", "internal/core"},
	// What a placement gives a container. Each kind imports the interface and core (and what it
	// reads a machine with), never another kind; only production names a concrete kind.
	"internal/resource":              {"internal/core"},
	"internal/resource/resourcetest": {"internal/core", "internal/resource"},
	"internal/resource/fakeresource": {"internal/core", "internal/resource"},
	"internal/resource/gpu":          {"internal/core", "internal/resource"},
	"internal/resource/rocm":         {"internal/core", "internal/resource"},
	"internal/resource/mounts":       {"internal/core", "internal/resource"},
	"internal/resource/kubernetes":   {"internal/clock", "internal/core", "internal/remote", "internal/resource"},
	"internal/environment":           {"internal/core"},
	"internal/environment/fakeenv":   {"internal/clock", "internal/core", "internal/environment"},
	// The fake machine's world for the contract suite. Only tests reach it.
	"internal/environment/fakeenv/fakeworld": {"internal/environment/envtest", "internal/environment/fakeenv"},
	"internal/environment/envtest":           {"internal/clock", "internal/core", "internal/environment"},
	"internal/environment/host":              {"internal/backend", "internal/clock", "internal/core", "internal/environment"},

	// The docker environment decides what to run and how it ends; an engine is the only thing that
	// talks to Docker. The real engine drives the docker program, the fake keeps a daemon in
	// memory, and one contract suite holds both to the same behavior.
	"internal/environment/docker":           {"internal/backend", "internal/clock", "internal/core", "internal/environment", "internal/resource", "internal/task"},
	"internal/environment/docker/dockercli": {"internal/clock", "internal/core", "internal/environment", "internal/environment/docker", "internal/remote"},
	// Where a docker engine's containers find a run's directories: this machine, or copies on one
	// reached over ssh.
	"internal/environment/docker/machine": {"internal/clock", "internal/core", "internal/environment/docker", "internal/remote"},
	// Containers and builds placed in Slurm allocations: a dockercli allocator, and an engine that
	// ends the allocations.
	"internal/environment/slurm": {"internal/clock", "internal/core", "internal/environment/docker", "internal/environment/docker/dockercli", "internal/remote"},
	"internal/environment/docker/dockerfake": {
		"internal/core", "internal/environment", "internal/environment/docker", "internal/environment/fakeenv",
	},
	// Containers placed on a Slurm cluster whose nodes run Apptainer and no Docker: an engine over
	// the cluster's interface, the real cluster reached over ssh, and a fake kept in memory.
	"internal/environment/apptainer": {
		"internal/backend", "internal/backend/cpu", "internal/clock", "internal/core", "internal/environment",
		"internal/environment/docker",
	},
	"internal/environment/apptainer/clustercli": {
		"internal/clock", "internal/core", "internal/environment", "internal/environment/apptainer",
		"internal/environment/docker", "internal/environment/slurm", "internal/remote",
	},
	"internal/environment/apptainer/clusterfake": {
		"internal/core", "internal/environment", "internal/environment/apptainer", "internal/environment/docker",
		"internal/environment/fakeenv",
	},
	"internal/environment/docker/dockertest": {"internal/core", "internal/environment", "internal/environment/docker", "internal/environment/envtest"},
	"internal/gitrepo":                       {"internal/core"},
	"internal/task":                          {"gen/", "internal/core", "internal/tomlproto"},
	"internal/task/tasktest":                 {"internal/core", "internal/task"},
	"internal/gitwork":                       {"internal/core", "internal/task"},
	"internal/reading":                       {},
	"internal/flow":                          {"internal/core", "internal/reading", "internal/task"},
	"internal/hostfs":                        {},
	"internal/flowrun": {
		"internal/clock", "internal/core", "internal/environment", "internal/flow", "internal/hostfs",
		"internal/reading",
	},
	"internal/runtimedir": {"gen/", "internal/clock", "internal/core"},

	// Storage.
	"internal/store":           {"internal/core"},
	"internal/store/ledger":    {"internal/core", "internal/store"},
	"internal/store/storetest": {"internal/core", "internal/store"},
	"internal/store/memstore":  {"internal/core", "internal/store", "internal/store/ledger"},
	// The files of a store are the contract's messages in protojson, so the filestore speaks gen/
	// and translates through wire; see storageEncoding for what it may not use instead.
	"internal/store/filestore":      {"gen/", "internal/core", "internal/store", "internal/store/ledger", "internal/store/filestore/fsys", "internal/wire"},
	"internal/store/filestore/fsys": {},

	// The translation between the domain and the contract.
	"internal/wire": {"internal/core", "internal/flow", "internal/lifecycle", "internal/task", "gen/"},

	// Supervisors: the domain, its channel to the daemon, and the process that serves it.
	"internal/supervisor": {
		"internal/changed", "internal/clock", "internal/core", "internal/environment", "internal/flow",
		"internal/flowrun", "internal/hostfs", "internal/lifecycle", "internal/owned",
	},
	"internal/supervisor/suptest": {"internal/core", "internal/lifecycle", "internal/owned", "internal/supervisor"},
	"internal/supervisor/rpc":     {"gen/", "internal/core", "internal/lifecycle", "internal/supervisor", "internal/wire"},
	"internal/supervisor/host": {
		"gen/", "internal/clock", "internal/core", "internal/environment", "internal/lifecycle", "internal/owned",
		"internal/runtimedir", "internal/supervisor", "internal/supervisor/rpc", "internal/wire",
	},

	// The daemon: its job flows, the process that serves them, and the client that calls it. The
	// client speaks the contract's messages only, so it does not import the domain.
	"internal/app": {
		"internal/changed", "internal/clock", "internal/core", "internal/environment", "internal/flow", "internal/gitrepo",
		"internal/lease", "internal/lifecycle", "internal/owned", "internal/schedule", "internal/store", "internal/supervisor",
		"internal/task",
	},
	"internal/app/filestores": {"internal/app", "internal/core", "internal/store", "internal/store/filestore"},
	"internal/app/memstores":  {"internal/app", "internal/core", "internal/store", "internal/store/memstore"},
	"internal/daemon": {
		"gen/", "internal/app", "internal/clock", "internal/core", "internal/environment", "internal/owned",
		"internal/gitrepo", "internal/runtimedir", "internal/supervisor", "internal/task", "internal/wire",
	},
	"internal/daemon/daemontest": {
		"internal/app", "internal/app/memstores", "internal/backend/cpu", "internal/clock", "internal/core",
		"internal/daemon", "internal/environment/fakeenv", "internal/gitrepo", "internal/hostfs", "internal/owned",
		"internal/runtimedir", "internal/supervisor", "internal/task",
	},
	"internal/client": {"gen/", "internal/clock", "internal/runtimedir", "internal/wire"},

	// The simulation of the daemon has tests and no product code.
	"internal/sim": {},

	// The wiring of the shipped binary.
	"internal/production": {
		"internal/app", "internal/app/filestores", "internal/backend/cpu", "internal/client", "internal/clock",
		"internal/core", "internal/daemon", "internal/environment", "internal/environment/docker",
		"internal/environment/docker/dockercli", "internal/environment/host", "internal/gitrepo",
		"internal/gitwork", "internal/hostfs", "internal/runtimedir", "internal/store/filestore",
		"internal/supervisor", "internal/supervisor/host", "internal/task",
		"internal/backend/nvidia", "internal/environment/docker/machine", "internal/environment/slurm",
		"internal/operator", "internal/remote", "internal/schedule", "internal/schedule/policy/fairshare",
		"internal/schedule/policy/fifo", "gen/", "internal/resource", "internal/resource/gpu",
		"internal/resource/kubernetes", "internal/resource/mounts", "internal/lease", "internal/lease/hostlease", "internal/lease/kubelease",
		"internal/lease/locallease", "internal/neighbors",
		"internal/backend/amd", "internal/resource/rocm", "internal/environment/apptainer", "internal/environment/apptainer/clustercli",
	},

	// Replacing the running binary with a newer release.
	"internal/upgrade":             {"internal/upgrade/upgradetest"},
	"internal/upgrade/upgradetest": {"internal/upgrade"},

	// Which waiting jobs start: a pure core that owns capacity and exclusivity, and the policies
	// that order the jobs, which only production names.
	"internal/schedule":                  {"internal/core"},
	"internal/schedule/scheduletest":     {"internal/core", "internal/schedule"},
	"internal/schedule/policy/fifo":      {"internal/schedule"},
	"internal/schedule/policy/fairshare": {"internal/core", "internal/schedule"},

	// Leases: how the daemons that share a resource take turns at it. Each place a lease is kept
	// is a package of its own, which only production names, and passes the contract in leasetest.
	"internal/lease":                    {"internal/core"},
	"internal/lease/leasetest":          {"internal/core", "internal/lease"},
	"internal/lease/fakelease":          {"internal/core", "internal/lease"},
	"internal/lease/locallease":         {"internal/core", "internal/lease"},
	"internal/lease/hostlease":          {"internal/core", "internal/lease", "internal/remote"},
	"internal/lease/kubelease":          {"internal/clock", "internal/core", "internal/lease", "internal/remote"},
	"internal/lease/kubelease/kubefake": {"internal/lease/kubelease"},

	// The daemons of the machine, as its proc file system lists them.
	"internal/neighbors": {},

	"internal/archcheck": {},
}

// testRules lists, for each package directory under cli/, the packages only its test files may
// import in addition to rules: the contract suite an implementation runs, a fake or another
// implementation to compare it with, and the packages a test composes a daemon from. Product code
// reaches none of them, so a fake never ships.
var testRules = map[string][]string{
	"internal/backend/cpu":         {"internal/backend/backendtest"},
	"internal/environment":         {"internal/environment/fakeenv"},
	"internal/environment/fakeenv": {"internal/environment/envtest", "internal/environment/fakeenv/fakeworld"},
	"internal/environment/host":    {"internal/backend/cpu", "internal/environment/envtest", "internal/environment/fakeenv"},
	"internal/environment/slurm": {
		"internal/backend/cpu", "internal/environment", "internal/environment/docker/dockerfake",
		"internal/environment/envtest", "internal/environment/fakeenv/fakeworld",
	},
	"internal/environment/docker/machine": {"internal/environment/envtest"},
	"internal/remote":                     {"internal/environment/envtest"},
	"internal/resource/gpu":               {"internal/resource/resourcetest"},
	"internal/resource/rocm":              {"internal/resource/resourcetest"},
	"internal/environment/apptainer": {
		"internal/backend/amd", "internal/environment/apptainer/clusterfake",
		"internal/environment/docker/dockerfake", "internal/environment/docker/dockertest", "internal/environment/envtest",
		"internal/environment/fakeenv", "internal/environment/fakeenv/fakeworld", "internal/resource",
		"internal/resource/rocm", "internal/task",
	},
	"internal/environment/apptainer/clustercli": {"internal/environment/envtest"},
	"internal/resource/mounts":                  {"internal/resource/resourcetest"},
	"internal/resource/kubernetes":              {"internal/resource/resourcetest"},
	"internal/resource/fakeresource":            {"internal/resource/resourcetest"},
	"internal/environment/docker": {
		"internal/resource/fakeresource",
		"internal/backend/cpu", "internal/clock", "internal/environment/docker/dockerfake",
		"internal/environment/docker/dockertest", "internal/environment/envtest", "internal/environment/fakeenv",
		"internal/environment/fakeenv/fakeworld", "internal/task", "internal/task/tasktest",
	},
	"internal/environment/docker/dockercli": {
		"internal/backend", "internal/backend/cpu", "internal/clock", "internal/environment/docker/dockertest",
		"internal/environment/envtest", "internal/resource", "internal/resource/fakeresource",
	},
	"internal/environment/docker/dockerfake": {
		"internal/environment/docker/dockertest", "internal/environment/envtest", "internal/environment/fakeenv/fakeworld",
	},
	"internal/store/ledger":    {"internal/clock"},
	"internal/hostfs":          {},
	"internal/flowrun":         {"internal/backend/cpu", "internal/environment/fakeenv", "internal/environment/host", "internal/task"},
	"internal/task":            {"internal/task/tasktest"},
	"internal/gitwork":         {"internal/task/tasktest"},
	"internal/store/memstore":  {"internal/store/storetest"},
	"internal/store/filestore": {"internal/store/storetest", "internal/store/memstore"},
	"internal/supervisor": {
		"internal/backend/cpu", "internal/environment/fakeenv", "internal/supervisor/suptest", "internal/task",
	},
	"internal/supervisor/rpc":  {"internal/environment/fakeenv", "internal/supervisor/suptest"},
	"internal/supervisor/host": {"internal/environment/fakeenv", "internal/supervisor/suptest"},
	"internal/app": {
		"internal/app/filestores", "internal/app/memstores", "internal/backend/cpu", "internal/environment/fakeenv",
		"internal/hostfs", "internal/store/filestore", "internal/store/filestore/fsys", "internal/store/storetest", "internal/task/tasktest",
		"internal/lease/fakelease",
	},
	"internal/app/filestores": {"internal/core"},
	"internal/daemon": {
		"internal/store", "internal/app/memstores", "internal/environment/fakeenv", "internal/store/storetest",
		"internal/lease", "internal/lease/fakelease", "internal/schedule",
	},
	"internal/client": {
		"internal/app", "internal/app/memstores", "internal/core", "internal/daemon", "internal/daemon/daemontest",
		"internal/environment/fakeenv", "internal/supervisor", "internal/task/tasktest",
	},
	// The whole daemon, over Fakes, in one process.
	"internal/sim": {
		"gen/", "internal/app", "internal/app/memstores", "internal/client", "internal/clock", "internal/core",
		"internal/daemon", "internal/environment/fakeenv", "internal/gitrepo", "internal/lifecycle", "internal/owned",
		"internal/runtimedir", "internal/schedule", "internal/schedule/policy/fairshare", "internal/schedule/policy/fifo",
		"internal/store", "internal/supervisor", "internal/lease", "internal/lease/fakelease",
	},
	"cmd/vibesys-cli": {
		"internal/app", "internal/app/filestores", "internal/clock", "internal/core", "internal/daemon",
		"internal/daemon/daemontest", "internal/environment/docker", "internal/environment/fakeenv",
		"internal/lifecycle", "internal/runtimedir", "internal/store", "internal/store/filestore",
		"internal/store/storetest", "internal/supervisor", "internal/supervisor/host",
		"internal/task", "internal/task/tasktest",
	},

	// The scheduler's contract suite, and the policies a test of the lifecycle runs it with.
	"internal/lifecycle":                 {"internal/schedule/policy/fairshare", "internal/schedule/policy/fifo"},
	"internal/schedule":                  {"internal/schedule/policy/fifo", "internal/schedule/scheduletest"},
	"internal/schedule/policy/fifo":      {"internal/schedule/scheduletest"},
	"internal/schedule/policy/fairshare": {"internal/schedule/scheduletest"},

	// The leases' contract suite, and the places a test of one compares it with.
	"internal/lease/fakelease":  {"internal/lease/leasetest"},
	"internal/lease/locallease": {"internal/lease/leasetest", "internal/runtimedir"},
	"internal/lease/hostlease":  {"internal/lease/leasetest", "internal/lease/locallease", "internal/runtimedir"},
	"internal/lease/kubelease":  {"internal/lease/kubelease/kubefake", "internal/lease/leasetest"},
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
			found = append(found, storageEncoding(name, source)...)
			found = append(found, effects(name, source)...)
			found = append(found, approvalMints(name, source)...)
			found = append(found, sweepViolations(name, source)...)
			found = append(found, scopeMints(name, source)...)
			found = append(found, spellings(name, source)...)
		case strings.HasSuffix(name, ".proto") && strings.HasPrefix(name, "proto/vibesys/cli/"):
			found = append(found, layout(name, source)...)
		}
	}
	found = append(found, decoderViolations(files)...)
	found = append(found, switchViolations(files)...)
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
