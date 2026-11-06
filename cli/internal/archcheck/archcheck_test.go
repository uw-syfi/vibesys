package archcheck_test

import (
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"
	"testing/quick"

	"github.com/uw-syfi/vibesys/cli/internal/archcheck"
)

func goFile(imports ...string) string {
	var b strings.Builder
	b.WriteString("package p\n\nimport (\n")
	for _, target := range imports {
		b.WriteString("\t_ \"" + target + "\"\n")
	}
	b.WriteString(")\n")
	return b.String()
}

const module = "github.com/uw-syfi/vibesys/cli/"

func TestLayerRules(t *testing.T) {
	cases := []struct {
		name  string
		files map[string]string
		bad   bool
	}{
		{"core imports only the standard library", map[string]string{"cli/internal/core/a.go": goFile("time")}, false},
		{"core cannot import the store", map[string]string{"cli/internal/core/a.go": goFile(module + "internal/store")}, true},
		{"the store imports core", map[string]string{"cli/internal/store/a.go": goFile(module + "internal/core")}, false},
		{"core cannot import generated types", map[string]string{"cli/internal/core/a.go": goFile(module + "gen/vibesys/cli/v1")}, true},
		{"wire imports core and gen", map[string]string{"cli/internal/wire/a.go": goFile(module+"internal/core", module+"gen/vibesys/cli/v1")}, false},
		{"the store cannot import generated types", map[string]string{"cli/internal/store/a.go": goFile(module + "gen/vibesys/cli/v1")}, true},
		{"the environment cannot import the store", map[string]string{"cli/internal/environment/a.go": goFile(module + "internal/store")}, true},
		{"filestore imports the store interface", map[string]string{"cli/internal/store/filestore/a.go": goFile(module + "internal/store")}, false},
		{"the store cannot import its implementation", map[string]string{"cli/internal/store/a.go": goFile(module + "internal/store/filestore")}, true},
		{"generated code imports only generated code", map[string]string{"cli/gen/vibesys/cli/v1/a.go": goFile(module + "internal/core")}, true},
		{"a package with no rule fails", map[string]string{"cli/internal/newthing/a.go": goFile("time")}, true},
		{"ledger imports core and the store", map[string]string{"cli/internal/store/ledger/a.go": goFile(module+"internal/core", module+"internal/store")}, false},
		{"the ledger cannot import an implementation", map[string]string{"cli/internal/store/ledger/a.go": goFile(module + "internal/store/memstore")}, true},
		{"memstore imports the ledger", map[string]string{"cli/internal/store/memstore/a.go": goFile(module + "internal/store/ledger")}, false},
		{"filestore imports the ledger and its filesystem seam", map[string]string{"cli/internal/store/filestore/a.go": goFile(module+"internal/store/ledger", module+"internal/store/filestore/fsys")}, false},
		{"the filesystem seam imports nothing of the module", map[string]string{"cli/internal/store/filestore/fsys/a.go": goFile(module + "internal/core")}, true},
		{"the ledger cannot import the filesystem seam", map[string]string{"cli/internal/store/ledger/a.go": goFile(module + "internal/store/filestore/fsys")}, true},
		{"memstore and filestore cannot import each other", map[string]string{"cli/internal/store/memstore/a.go": goFile(module + "internal/store/filestore")}, true},
		{"filestore code cannot import the contract suite", map[string]string{"cli/internal/store/filestore/a.go": goFile(module + "internal/store/storetest")}, true},
		{"memstore code cannot import the contract suite", map[string]string{"cli/internal/store/memstore/a.go": goFile(module + "internal/store/storetest")}, true},
		{"a memstore test runs the contract suite", map[string]string{"cli/internal/store/memstore/a_test.go": goFile(module + "internal/store/storetest")}, false},
		{"a filestore test runs the suite and compares with memstore", map[string]string{"cli/internal/store/filestore/a_test.go": goFile(module+"internal/store/storetest", module+"internal/store/memstore")}, false},
		{"a memstore test cannot import filestore", map[string]string{"cli/internal/store/memstore/a_test.go": goFile(module + "internal/store/filestore")}, true},
		{"the contract suite imports core and the store", map[string]string{"cli/internal/store/storetest/a.go": goFile(module+"internal/core", module+"internal/store")}, false},
		{"the contract suite cannot import an implementation", map[string]string{"cli/internal/store/storetest/a.go": goFile(module + "internal/store/memstore")}, true},
		{"the command line cannot import the domain", map[string]string{"cli/cmd/vibesys-cli/a.go": goFile(module + "internal/core")}, true},
		{"the command line imports the client", map[string]string{"cli/cmd/vibesys-cli/a.go": goFile(module+"internal/client", module+"gen/vibesys/cli/v1", module+"internal/wire")}, false},
		{"the command line cannot import the daemon", map[string]string{"cli/cmd/vibesys-cli/a.go": goFile(module + "internal/daemon")}, true},
		{"the client cannot import the domain", map[string]string{"cli/internal/client/a.go": goFile(module + "internal/core")}, true},
		{"the client cannot import the daemon", map[string]string{"cli/internal/client/a.go": goFile(module + "internal/daemon")}, true},
		{"a daemon test composes a store interface", map[string]string{"cli/internal/daemon/a_test.go": goFile(module + "internal/store")}, false},
		{"daemon product code cannot import stores", map[string]string{"cli/internal/daemon/a.go": goFile(module + "internal/store")}, true},
		{"the daemon cannot import the client", map[string]string{"cli/internal/daemon/a.go": goFile(module + "internal/client")}, true},
		{"the daemon imports the job flows", map[string]string{"cli/internal/daemon/a.go": goFile(module+"internal/app", module+"internal/wire")}, false},
		{"the job flows cannot import the daemon", map[string]string{"cli/internal/app/a.go": goFile(module + "internal/daemon")}, true},
		{"the job flows cannot import generated types", map[string]string{"cli/internal/app/a.go": goFile(module + "gen/vibesys/cli/v1")}, true},
		{"the lifecycle machine cannot import the store", map[string]string{"cli/internal/lifecycle/a.go": goFile(module + "internal/store")}, true},
		{"the domain cannot import the lifecycle machine", map[string]string{"cli/internal/core/a.go": goFile(module + "internal/lifecycle")}, true},
		{"a supervisor cannot import the daemon", map[string]string{"cli/internal/supervisor/a.go": goFile(module + "internal/daemon")}, true},
		{"the supervisor domain cannot import its transport", map[string]string{"cli/internal/supervisor/a.go": goFile(module + "internal/supervisor/rpc")}, true},
		{"the transport imports the supervisor domain", map[string]string{"cli/internal/supervisor/rpc/a.go": goFile(module+"internal/supervisor", module+"internal/wire")}, false},
		{"the wiring imports the daemon and the client", map[string]string{"cli/internal/production/a.go": goFile(module+"internal/daemon", module+"internal/client")}, false},
		{"nothing but the wiring and the command line imports the wiring", map[string]string{"cli/internal/daemon/a.go": goFile(module + "internal/production")}, true},
		{"an app test loads historical files through fake FS", map[string]string{"cli/internal/app/a_test.go": goFile(module + "internal/store/filestore/fsys")}, false},
		{"app product code cannot import filesystem implementation", map[string]string{"cli/internal/app/a.go": goFile(module + "internal/store/filestore/fsys")}, true},
		{"a daemon test composes fakes", map[string]string{"cli/internal/daemon/a_test.go": goFile(module + "internal/environment/fakeenv")}, false},
		{"daemon code cannot import a fake", map[string]string{"cli/internal/daemon/a.go": goFile(module + "internal/environment/fakeenv")}, true},
		{"the wiring cannot import a fake", map[string]string{"cli/internal/production/a.go": goFile(module + "internal/environment/fakeenv")}, true},
		{"the wiring cannot import the test daemon", map[string]string{"cli/internal/production/a.go": goFile(module + "internal/daemon/daemontest")}, true},
		{"the command line's tests compose a daemon", map[string]string{"cli/cmd/vibesys-cli/a_test.go": goFile(module+"internal/daemon/daemontest", module+"internal/core")}, false},
		{"an unparsable file fails", map[string]string{"cli/internal/core/a.go": "package ("}, true},
	}
	for _, c := range cases {
		if got := archcheck.Violations(c.files); (len(got) > 0) != c.bad {
			t.Errorf("%s: violations = %v, want bad=%v", c.name, got, c.bad)
		}
	}
}

// Whatever names the layout is flagged outside filestore and allowed inside it, in Go and proto.
func TestLayoutNamesStayInsideFilestore(t *testing.T) {
	names := []string{
		`".vibesys-state"`, `"events.jsonl"`, `"~/.local/state/vibesys"`, `os.Getenv("XDG_STATE_HOME")`,
		`"results/" + key`, `filepath.Join(dir, "runs", id)`, `path.Join("logs", id)`,
		`"jobs/" + id`, `"plans/1.json"`, `path.Join(root, "promotions")`, `filepath.Join(dir, "jobs", id)`,
	}
	property := func(pick uint8, before, after string) bool {
		source := "package p\n\nvar _ = " + before + names[int(pick)%len(names)] + after + "\n"
		outside := archcheck.Violations(map[string]string{"cli/internal/core/a.go": source})
		inside := archcheck.Violations(map[string]string{"cli/internal/store/filestore/a.go": source})
		return hasLayout(outside) && !hasLayout(inside)
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
	proto := archcheck.Violations(map[string]string{"proto/vibesys/cli/v1/a.proto": "// stored in events.jsonl\n"})
	if !hasLayout(proto) {
		t.Errorf("a proto comment naming the layout was not flagged: %v", proto)
	}
}

// A store persists protojson of its messages, and describes no record by hand: no package under
// internal/store, tests included, imports a hand encoding, and a package outside the store may.
func TestStorePackagesImportNoHandEncoding(t *testing.T) {
	encodings := []string{"encoding/json", "encoding/gob", "encoding/xml", "encoding/csv", "github.com/BurntSushi/toml"}
	stores := []string{
		"cli/internal/store/store.go", "cli/internal/store/ledger/a.go", "cli/internal/store/memstore/a_test.go",
		"cli/internal/store/filestore/records.go", "cli/internal/store/filestore/owner_test.go", "cli/internal/store/filestore/fsys/a.go",
		"cli/internal/store/storetest/a.go",
	}
	property := func(pickEncoding, pickFile uint8) bool {
		encoding, file := encodings[int(pickEncoding)%len(encodings)], stores[int(pickFile)%len(stores)]
		violations := archcheck.Violations(map[string]string{file: goFile(encoding, "google.golang.org/protobuf/encoding/protojson")})
		flagged := 0
		for _, line := range violations {
			if strings.Contains(line, file) && strings.Contains(line, "imports "+encoding+";") {
				flagged++
			}
		}
		outside := archcheck.Violations(map[string]string{"cli/internal/reading/a.go": goFile(encoding)})
		return flagged == 1 && !slices.ContainsFunc(outside, func(line string) bool { return strings.Contains(line, "protojson of a message") })
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
	for _, file := range stores {
		if v := archcheck.Violations(map[string]string{file: goFile("google.golang.org/protobuf/encoding/protojson")}); slices.ContainsFunc(v, func(l string) bool { return strings.Contains(l, "hand") }) {
			t.Errorf("protojson was flagged in %s: %v", file, v)
		}
	}
}

func hasLayout(violations []string) bool {
	for _, line := range violations {
		if strings.Contains(line, "names the storage layout") {
			return true
		}
	}
	return false
}

// The real tree obeys the rules.
func TestRepositoryObeysTheRules(t *testing.T) {
	for _, line := range archcheck.Violations(repositorySources(t)) {
		t.Error(line)
	}
}

// Every listed exception still excuses a violation, so fixing one removes its entry.
func TestExceptionsAreStillNeeded(t *testing.T) {
	for _, line := range archcheck.StaleExceptions(repositorySources(t)) {
		t.Error(line)
	}
	clockList, goList := archcheck.Exceptions()
	for _, e := range append(clockList, goList...) {
		if strings.TrimSpace(e.Reason) == "" {
			t.Errorf("%s: an exception states no reason", e.File)
		}
	}
}

func repositorySources(t *testing.T) map[string]string {
	t.Helper()
	root, err := filepath.Abs("../../..")
	if err != nil {
		t.Fatal(err)
	}
	files := map[string]string{}
	for _, tree := range []string{"cli", "proto/vibesys/cli"} {
		err := filepath.WalkDir(filepath.Join(root, tree), func(p string, d fs.DirEntry, err error) error {
			if err != nil || d.IsDir() {
				return err
			}
			if ext := path.Ext(p); ext != ".go" && ext != ".proto" {
				return nil
			}
			source, err := os.ReadFile(p)
			if err != nil {
				return err
			}
			rel, err := filepath.Rel(root, p)
			if err != nil {
				return err
			}
			files[filepath.ToSlash(rel)] = string(source)
			return nil
		})
		if err != nil {
			t.Fatal(err)
		}
	}
	if len(files) == 0 {
		t.Fatal("found no sources to check")
	}
	return files
}

// Product code never imports a package that exists for tests: a fake, a contract suite or a test
// daemon. Each support package is allowed to be imported by test files only, in every package.
func TestSupportPackagesAreImportedByTestsOnly(t *testing.T) {
	support := []string{
		"internal/environment/fakeenv", "internal/supervisor/suptest", "internal/daemon/daemontest",
		"internal/store/storetest", "internal/environment/envtest", "internal/backend/backendtest",
		"internal/environment/fakeenv/fakeworld", "internal/environment/docker/dockerfake",
		"internal/environment/docker/dockertest", "internal/resource/resourcetest", "internal/resource/fakeresource",
		"internal/environment/apptainer/clusterfake",
	}
	var productDirs []string
	for dir := range archcheck.Rules() {
		productDirs = append(productDirs, dir)
	}
	for _, target := range support {
		for _, dir := range productDirs {
			if dir == target || slices.Contains(support, dir) || strings.HasSuffix(dir, "test") || strings.HasSuffix(dir, "fakeenv") {
				continue
			}
			got := archcheck.Violations(map[string]string{"cli/" + dir + "/a.go": goFile(module + target)})
			if len(got) == 0 {
				t.Errorf("product code in %s may import the test support package %s", dir, target)
			}
		}
	}
}

func hasLine(violations []string, part string) bool {
	for _, line := range violations {
		if strings.Contains(line, part) {
			return true
		}
	}
	return false
}

func withTime(import_, body string) string {
	return "package p\n\nimport " + import_ + "\n\nfunc f() {\n\t" + body + "\n}\n"
}

// Every function that reads or waits on the real clock is refused outside internal/clock, however
// the package is imported, in product code and in tests; internal/clock itself may use them.
func TestRealClockStaysInsideTheClockPackage(t *testing.T) {
	calls := []string{"Now()", "Since(x)", "Until(x)", "Sleep(d)", "After(d)", "AfterFunc(d, g)", "Tick(d)", "NewTimer(d)", "NewTicker(d)"}
	files := []string{"cli/internal/core/a.go", "cli/internal/app/a.go", "cli/internal/daemon/a_test.go", "cli/cmd/vibesys-cli/a.go"}
	property := func(pick, where uint8, aliased bool) bool {
		call := calls[int(pick)%len(calls)]
		name := files[int(where)%len(files)]
		qualifier, spec := "time", `"time"`
		if aliased {
			qualifier, spec = "tm", `tm "time"`
		}
		source := withTime(spec, "_ = "+qualifier+"."+call)
		outside := archcheck.Violations(map[string]string{name: source})
		inside := archcheck.Violations(map[string]string{"cli/internal/clock/a.go": source})
		return hasLine(outside, "the real clock is reached only through internal/clock") && !hasLine(inside, "real clock")
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
}

// Values and constructors that read no clock are allowed, and so is a local named time.
func TestPureTimeUseIsAllowed(t *testing.T) {
	sources := []string{
		withTime(`"time"`, "_ = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)"),
		withTime(`"time"`, "_ = time.Unix(1, 2).In(time.FixedZone(\"z\", 3600)).Add(time.Second)"),
		withTime(`"time"`, "var d time.Duration = 5 * time.Minute; _ = d"),
		withTime(`"time"`, "_, _ = time.ParseDuration(\"1s\")"),
		"package p\n\ntype clk struct{}\n\nfunc (clk) Now() int { return 0 }\n\nfunc f() { time := clk{}; _ = time.Now() }\n",
		withTime(`"fmt"`, "_ = fmt.Sprint()"),
	}
	for _, source := range sources {
		if got := archcheck.Violations(map[string]string{"cli/internal/core/a.go": source}); len(got) > 0 {
			t.Errorf("%q: violations = %v, want none", source, got)
		}
	}
}

func TestGoStatementsStartThroughTheOwnedGroup(t *testing.T) {
	bare := "package p\n\nfunc f() {\n\tgo func() {}()\n}\n"
	cases := []struct {
		name string
		bad  bool
	}{
		{"cli/internal/app/a.go", true},
		{"cli/internal/app/filestores/a.go", true},
		{"cli/internal/daemon/a.go", true},
		{"cli/internal/daemon/daemontest/a.go", true},
		{"cli/internal/supervisor/a.go", true},
		{"cli/internal/supervisor/rpc/a.go", true},
		{"cli/internal/supervisor/suptest/a.go", true},
		// A test starts its own client goroutines, bounded by the concurrency kit.
		{"cli/internal/app/a_test.go", false},
		{"cli/internal/daemon/a_test.go", false},
		// Outside the daemon's packages the rule does not apply, and the group is built on goroutines.
		{"cli/internal/owned/a.go", false},
		{"cli/internal/runtimedir/a.go", false},
		{"cli/internal/store/storetest/a.go", false},
	}
	for _, c := range cases {
		got := archcheck.Violations(map[string]string{c.name: bare})
		if hasLine(got, "bare go statement") != c.bad {
			t.Errorf("%s: violations = %v, want bad=%v", c.name, got, c.bad)
		}
	}
	grouped := "package p\n\nimport \"" + module + "internal/owned\"\n\nfunc f() {\n\tvar g owned.Group\n\tg.Go(func() {})\n\tg.Wait()\n}\n"
	if got := archcheck.Violations(map[string]string{"cli/internal/app/a.go": grouped}); len(got) > 0 {
		t.Errorf("a goroutine started through owned.Group was flagged: %v", got)
	}
}

// An exception excuses only its own file, and goes stale when its file stops breaking the rule.
func TestExceptionsExcuseOnlyTheirFile(t *testing.T) {
	clockList, _ := archcheck.Exceptions()
	if len(clockList) == 0 {
		t.Fatal("expected the clock rule to list an exception")
	}
	clockBad := withTime(`"time"`, "_ = time.Now()")
	if got := archcheck.Violations(map[string]string{clockList[0].File: clockBad}); hasLine(got, "real clock") {
		t.Errorf("an excepted file was flagged: %v", got)
	}
	if got := archcheck.Violations(map[string]string{clockList[0].File + "x.go": clockBad}); !hasLine(got, "real clock") {
		t.Errorf("a file beside an excepted one was not flagged: %v", got)
	}
	clean := "package p\n"
	files := map[string]string{clockList[0].File: clean}
	stale := archcheck.StaleExceptions(files)
	if !hasLine(stale, clockList[0].File+": the clock exception is no longer needed") {
		t.Errorf("a fixed clock exception was not reported stale: %v", stale)
	}
	files[clockList[0].File] = withTime(`"time"`, "_ = time.Now()")
	if stale := archcheck.StaleExceptions(files); hasLine(stale, clockList[0].File+": the clock exception is no longer needed") {
		t.Errorf("a needed clock exception was reported stale: %v", stale)
	}
	delete(files, clockList[0].File)
	if stale := archcheck.StaleExceptions(files); !hasLine(stale, "names a file that does not exist") {
		t.Errorf("a missing excepted file was not reported: %v", stale)
	}
}

func wireSource(decoders ...string) string {
	var b strings.Builder
	b.WriteString("package wire\n")
	for _, d := range decoders {
		b.WriteString("\nfunc " + d + "() {}\n")
	}
	return b.String()
}

// A decoder of what a peer sent needs a fuzz target that calls it, unless it is listed with a
// reason, and a listed decoder that is gone is stale.
func TestEveryWireDecoderIsFuzzed(t *testing.T) {
	const fuzz = "cli/internal/wire/fuzz_test.go"
	unfuzzed := archcheck.UnfuzzedDecoders()
	for name, reason := range unfuzzed {
		if strings.TrimSpace(reason) == "" {
			t.Errorf("%s: an unfuzzed decoder states no reason", name)
		}
	}
	all := []string{"ToThing", "Thing"}
	for name := range unfuzzed {
		all = append(all, name)
	}
	cases := []struct {
		name  string
		files map[string]string
		bad   bool
	}{
		{"a decoder that a fuzz target calls", map[string]string{"cli/internal/wire/a.go": wireSource(all...), fuzz: "package wire_test\nvar _ = wire.ToThing(x)\n"}, false},
		{"a decoder no fuzz target calls", map[string]string{"cli/internal/wire/a.go": wireSource(all...), fuzz: "package wire_test\n"}, true},
		{"a decoder with no fuzz file at all", map[string]string{"cli/internal/wire/a.go": wireSource(all...)}, true},
		{"an encoder needs no target", map[string]string{"cli/internal/wire/a.go": wireSource(append([]string{"Thing"}, keys(unfuzzed)...)...), fuzz: "package wire_test\n"}, false},
		{"a listed decoder is stale once wire no longer defines it", map[string]string{"cli/internal/wire/a.go": wireSource("ToThing"), fuzz: "package wire_test\nvar _ = wire.ToThing(x)\n"}, true},
		{"a name in a test file is not a decoder", map[string]string{"cli/internal/wire/a_test.go": wireSource("ToThing")}, false},
	}
	for _, c := range cases {
		if got := archcheck.Violations(c.files); (len(got) > 0) != c.bad {
			t.Errorf("%s: violations = %v, want bad=%v", c.name, got, c.bad)
		}
	}
}

func keys(m map[string]string) []string {
	var out []string
	for k := range m {
		out = append(out, k)
	}
	return out
}

// exhaustiveTree is a small tree with a union, an enum and one switch over each.
func exhaustiveTree(body string) map[string]string {
	return map[string]string{
		"cli/internal/lifecycle/a.go": `package lifecycle

type Event interface{ event() }
type Started struct{}
type Ended struct{}

func (Started) event() {}
func (Ended) event()   {}

type Phase string

const (
	PhaseUp   Phase = "up"
	PhaseDown Phase = "down"
)
`,
		"cli/internal/core/b.go": "package core\n\nimport \"github.com/uw-syfi/vibesys/cli/internal/lifecycle\"\n\n" +
			"func f(e lifecycle.Event, p lifecycle.Phase) {\n" + body + "\n}\n",
	}
}

func hasSwitchViolation(found []string, naming string) bool {
	return slices.ContainsFunc(found, func(line string) bool {
		return strings.Contains(line, "a switch over") && strings.Contains(line, naming)
	})
}

// A switch over a closed set names every member or has a default, and one found by its marker
// needs no list, so a member added later is held too.
func TestASwitchOverAClosedSetLeavesNothingOut(t *testing.T) {
	for name, tc := range map[string]struct {
		body string
		bad  string
	}{
		"a union with a member left out":  {"switch e.(type) {\ncase lifecycle.Started:\n}", "lifecycle.Ended"},
		"an enum with a member left out":  {"switch p {\ncase lifecycle.PhaseUp:\n}", "lifecycle.PhaseDown"},
		"a union with every member":       {"switch e.(type) {\ncase lifecycle.Started, lifecycle.Ended:\n}", ""},
		"an enum with every member":       {"switch p {\ncase lifecycle.PhaseUp:\ncase lifecycle.PhaseDown:\n}", ""},
		"a default says what the rest do": {"switch e.(type) {\ncase lifecycle.Started:\ndefault:\n}", ""},
		"a switch on something else":      {"switch x := 1; x {\ncase 1:\n}", ""},
		"a switch with no tag":            {"switch {\ncase p == lifecycle.PhaseUp:\n}", ""},
	} {
		t.Run(name, func(t *testing.T) {
			found := archcheck.Violations(exhaustiveTree(tc.body))
			if tc.bad == "" {
				if hasSwitchViolation(found, "") {
					t.Fatalf("flagged: %v", found)
				}
				return
			}
			if !hasSwitchViolation(found, tc.bad) {
				t.Fatalf("not flagged: %v", found)
			}
		})
	}
}

// Only a test, the contract suite for stores and core itself may make an approval or a frontier
// admission without a decision; product code anywhere else may not, however it is written.
func TestApprovalWithoutADecisionStaysInTests(t *testing.T) {
	flagged := func(files map[string]string, mint string) bool {
		for _, line := range archcheck.Violations(files) {
			if strings.Contains(line, mint) {
				return true
			}
		}
		return false
	}
	mints := []struct{ name, call string }{
		{"AssumeApproved", "core.AssumeApproved(core.Promotion{Commit: %q})"},
		{"AssumeAdmitted", "core.AssumeAdmitted(1, []core.FrontierMember{{Commit: %q}})"},
	}
	property := func(pick, which uint8, arg string) bool {
		packages := []string{"app", "daemon", "wire", "store/ledger", "store/memstore", "store/filestore"}
		dir := "cli/internal/" + packages[int(pick)%len(packages)]
		mint := mints[int(which)%len(mints)]
		source := "package p\n\nimport \"github.com/uw-syfi/vibesys/cli/internal/core\"\n\nvar _ = " + fmt.Sprintf(mint.call, arg) + "\n"
		return flagged(map[string]string{dir + "/a.go": source}, mint.name) &&
			!flagged(map[string]string{dir + "/a_test.go": source}, mint.name) &&
			!flagged(map[string]string{"cli/internal/store/storetest/a.go": source}, mint.name)
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
}

// A type may belong to two closed sets, as a refusal is both a promotion decision and an event
// detail. A switch that names one set whole is not held to the other for the shared member, and a
// switch that names the shared member alone is held to both.
func TestASwitchOverTwoOverlappingSetsIsHeldToTheOneItNames(t *testing.T) {
	tree := func(body string) map[string]string {
		return map[string]string{
			"cli/internal/lifecycle/a.go": `package lifecycle

type Event interface{ event() }
type Answer interface{ answer() }
type Started struct{}
type Ended struct{}
type Done struct{}

func (Started) event() {}
func (Ended) event()   {}
func (Ended) answer()  {}
func (Done) answer()   {}
`,
			"cli/internal/core/b.go": "package core\n\nimport \"github.com/uw-syfi/vibesys/cli/internal/lifecycle\"\n\n" +
				"func f(e lifecycle.Event) {\n" + body + "\n}\n",
		}
	}
	for name, tc := range map[string]struct{ body, bad string }{
		"the first set whole":    {"switch e.(type) {\ncase lifecycle.Started:\ncase lifecycle.Ended:\n}", ""},
		"the second set whole":   {"switch e.(type) {\ncase lifecycle.Ended:\ncase lifecycle.Done:\n}", ""},
		"the shared member only": {"switch e.(type) {\ncase lifecycle.Ended:\n}", "lifecycle.Done"},
		"a member of one set":    {"switch e.(type) {\ncase lifecycle.Done:\n}", "lifecycle.Ended"},
	} {
		t.Run(name, func(t *testing.T) {
			found := archcheck.Violations(tree(tc.body))
			if tc.bad == "" && hasSwitchViolation(found, "") {
				t.Fatalf("flagged: %v", found)
			}
			if tc.bad != "" && !hasSwitchViolation(found, tc.bad) {
				t.Fatalf("not flagged: %v", found)
			}
		})
	}
}

// The race detector runs on every test except the sweeps, which walk a space of cases on one
// goroutine. What marks a sweep is its name and its build constraint together, so neither can be
// changed alone, and a sweep that gains a goroutine is refused.
func TestSweepsAreNamedBuiltWithoutRaceAndRunNoGoroutines(t *testing.T) {
	const pkg = "package p\n"
	cases := []struct {
		name, source string
		bad          string // part of the violation wanted, or "" for none
	}{
		{"cli/internal/core/x_sweep_test.go", "//go:build !race\n\n" + pkg, ""},
		{"cli/internal/core/x_sweep_test.go", "//go:build !race && linux\n\n" + pkg, ""},
		{"cli/internal/core/x_test.go", pkg, ""},
		{"cli/internal/core/x_test.go", "//go:build docker\n\n" + pkg, ""},
		{"cli/internal/core/x_test.go", "//go:build race\n\n" + pkg, ""},
		{"cli/internal/core/x_test.go", "//go:build !race\n\n" + pkg, "is a sweep; name it"},
		{"cli/internal/core/x_sweep_test.go", pkg, "must carry //go:build !race"},
		{"cli/internal/core/x_sweep_test.go", "//go:build docker\n\n" + pkg, "must carry //go:build !race"},
		{"cli/internal/core/x_sweep_test.go", "//go:build race\n\n" + pkg, "must carry //go:build !race"},
		{"cli/internal/core/x_sweep_test.go", "//go:build !race\n\n" + pkg + "func f() { go func() {}() }\n", "starts a goroutine"},
		{"cli/internal/core/x_sweep_test.go", "//go:build !race\n\n" + pkg + "import \"sync\"\n\nvar _ sync.Mutex\n", "imports sync"},
		{"cli/internal/core/x_sweep_test.go", "//go:build !race\n\n" + pkg + "import \"sync/atomic\"\n\nvar _ atomic.Int64\n", "imports sync/atomic"},
		// The convention is about tests: product code is never built conditionally on the detector here.
		{"cli/internal/core/x.go", "//go:build !race\n\n" + pkg, ""},
	}
	for _, c := range cases {
		got := archcheck.Violations(map[string]string{c.name: c.source})
		if c.bad == "" && len(got) > 0 || c.bad != "" && !hasLine(got, c.bad) {
			t.Errorf("%s with %q: violations = %v, want %q", c.name, c.source, got, c.bad)
		}
	}
}

// A store key is made only where a scope is resolved, or read back from what resolution wrote:
// product code anywhere else may not name ResolvedScope, however it is written.
func TestAScopeIsMadeOnlyByResolution(t *testing.T) {
	flagged := func(files map[string]string) bool {
		return slices.ContainsFunc(archcheck.Violations(files), func(line string) bool { return strings.Contains(line, "ResolvedScope") })
	}
	property := func(pick uint8, repo string) bool {
		packages := []string{"app", "daemon", "wire", "client", "app/memstores", "store/filestore"}
		dir := "cli/internal/" + packages[int(pick)%len(packages)]
		source := "package p\n\nimport \"github.com/uw-syfi/vibesys/cli/internal/core\"\n\nvar _, _ = core.ResolvedScope(" + fmt.Sprintf("%q", repo) + ", \"task\")\n"
		return flagged(map[string]string{dir + "/a.go": source}) &&
			!flagged(map[string]string{dir + "/a_test.go": source}) &&
			!flagged(map[string]string{"cli/internal/app/scope.go": source}) &&
			!flagged(map[string]string{"cli/internal/core/a.go": source})
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
}

// The contract's enums are closed sets too, read from the generated code: a switch over a failure
// kind that leaves one out, with no default, is flagged, and one that names them all is not.
func TestASwitchOverAContractEnumNamesEveryValue(t *testing.T) {
	gen := "cli/gen/vibesys/cli/v1/failure.pb.go"
	source, ok := repositorySources(t)[gen]
	if !ok {
		t.Fatalf("%s is not in the tree", gen)
	}
	switchOver := func(cases string) map[string]string {
		return map[string]string{gen: source, "cli/cmd/vibesys-cli/a.go": `package main

import cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"

func f(k cliv1.FailureKind) int {
	switch k {
	case ` + cases + `:
		return 1
	}
	return 4
}
`}
	}
	all := []string{}
	for _, v := range []string{"UNSPECIFIED", "INVALID_INPUT", "NOT_FOUND", "CONFLICT", "UNAVAILABLE", "INCOMPATIBLE", "ENVIRONMENT_MISMATCH", "NOT_MEASURED", "INTERNAL"} {
		all = append(all, "cliv1.FailureKind_FAILURE_KIND_"+v)
	}
	if found := archcheck.Violations(switchOver(strings.Join(all, ", "))); hasSwitchViolation(found, "FailureKind") {
		t.Fatalf("a switch naming every kind was flagged: %v", found)
	}
	missing := archcheck.Violations(switchOver(strings.Join(all[:len(all)-1], ", ")))
	if !hasSwitchViolation(missing, "FAILURE_KIND_INTERNAL") {
		t.Fatalf("a switch leaving INTERNAL out was not flagged: %v", missing)
	}
}

// A string that spells a concrete placement kind, resource kind or vendor is refused outside the
// package that implements it and the decoding of the files that name it, in any case; an import
// path, a comment, a test and the allowed places are not.
func TestConcreteNamesAreSpelledOnlyWhereTheyAreImplemented(t *testing.T) {
	file := func(lit string) string {
		return "package p\n\n// A comment may say kubernetes, slurm or nvidia.\nfunc f() string { return " + lit + " }\n"
	}
	for _, spelling := range archcheck.Spellings() {
		for _, lit := range []string{strconv.Quote(spelling), strconv.Quote("the " + strings.ToUpper(spelling) + " way"), "`" + spelling + "`"} {
			for _, at := range []string{"cli/internal/app/a.go", "cli/internal/environment/docker/a.go", "cli/internal/production/a.go", "cli/internal/core/a.go"} {
				if got := archcheck.Violations(map[string]string{at: file(lit)}); !hasLine(got, at+": a string spells "+strconv.Quote(spelling)) {
					t.Errorf("%s holding %s: %v, want the spelling refused", at, lit, got)
				}
			}
		}
	}
	allowed := map[string]string{
		"cli/internal/resource/kubernetes/a.go":     `"kubernetes"`,
		"cli/internal/operator/a.go":                `"slurm and kubernetes"`,
		"cli/internal/environment/slurm/a.go":       `"slurm"`,
		"cli/internal/resource/gpu/a.go":            `"NVIDIA_VISIBLE_DEVICES"`,
		"cli/internal/backend/nvidia/a.go":          `"nvidia-smi"`,
		"cli/internal/core/placement.go":            `"kubernetes"`,
		"cli/internal/environment/docker/a_test.go": `"nvidia"`,
	}
	for at, lit := range allowed {
		if got := archcheck.Violations(map[string]string{at: file(lit)}); hasLine(got, "a string spells") {
			t.Errorf("%s holding %s: %v, want it allowed", at, lit, got)
		}
	}
	imports := "package p\n\nimport (\n\t_ \"" + module + "internal/environment/slurm\"\n\t_ \"" + module + "internal/resource/kubernetes\"\n)\n"
	if got := archcheck.Violations(map[string]string{"cli/internal/production/a.go": imports}); hasLine(got, "a string spells") {
		t.Errorf("production importing the packages: %v, want it allowed", got)
	}
}
