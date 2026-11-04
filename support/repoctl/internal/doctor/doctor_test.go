package doctor_test

import (
	"bytes"
	"errors"
	"fmt"
	"math/rand"
	"strings"
	"testing"
	"testing/quick"

	"repoctl/internal/doctor"
)

const root = "/work/repo"

const policy = `
[hooks]
path = ".githooks"

[temp]
max_path_length = 40
candidates = ["$LOCAL_TMP", "/var/tmp/repo-$USER"]

[[programs]]
name = "python3"
minimum_from = { file = "pyproject.toml", pattern = 'requires-python = ">=([0-9.]+)"' }
repo_directories = [".venv/bin"]
fix = ["uv", "sync"]

[[programs]]
name = "bun"
exact_from = { file = "pin.py", pattern = 'BUN = "([0-9.]+)"' }
hint = "install the pinned bun"

[[programs]]
name = "buf"
repo_directories = [".venv/bin"]
fix = ["uv", "sync", "--group", "proto"]

[[installs]]
name = "deps"
directory = "clients/node_modules"
marker = "clients/node_modules/.modules.yaml"
command = ["pnpm", "install", "--frozen-lockfile"]
command_directory = "clients"

[[env_flags]]
name = "GOFLAGS"
flag = "-buildvcs=false"
linked_worktree_only = true
reason = "builds in a worktree fail"

[instructions]
files = ["AGENTS.md"]
roots = ["scripts/", "support/", ".githooks/"]
`

func config(t *testing.T) doctor.Config {
	t.Helper()
	cfg, err := doctor.ParseConfig("test", policy)
	if err != nil {
		t.Fatal(err)
	}
	return cfg
}

// goodHost is a machine on which every check passes.
func goodHost() *doctor.FakeHost {
	h := doctor.NewFakeHost()
	h.Env["PATH"] = "/usr/bin:/opt/bun/bin"
	h.Env["TMPDIR"] = "/scratch/tmp"
	h.Env["GOFLAGS"] = "-buildvcs=false"
	h.Linked = true
	h.Mounts["/"] = doctor.Filesystem{Type: "ext4", Device: 1}
	h.Files[root+"/pyproject.toml"] = `requires-python = ">=3.12"`
	h.Files[root+"/pin.py"] = `BUN = "1.4.2"`
	h.Files[root+"/AGENTS.md"] = "Run `./scripts/check.sh` and `support/tool`."
	h.Files[root+"/scripts/check.sh"] = ""
	h.Files[root+"/support/tool"] = ""
	h.Files["/usr/bin/python3"] = ""
	h.Versions["/usr/bin/python3"] = "Python 3.12.3"
	h.Files["/opt/bun/bin/bun"] = ""
	h.Versions["/opt/bun/bin/bun"] = "1.4.2"
	h.Files["/usr/bin/buf"] = ""
	h.Files[root+"/clients/node_modules/.modules.yaml"] = ""
	h.Config["core.hooksPath"] = ".githooks"
	return h
}

func evaluate(t *testing.T, h *doctor.FakeHost) []doctor.Finding {
	t.Helper()
	cfg := config(t)
	return doctor.Evaluate(cfg, doctor.Gather(cfg, root, h))
}

func find(t *testing.T, findings []doctor.Finding, check string) doctor.Finding {
	t.Helper()
	for _, f := range findings {
		if f.Check == check {
			return f
		}
	}
	t.Fatalf("no finding for %q in %v", check, findings)
	return doctor.Finding{}
}

func TestAGoodMachinePassesEveryCheck(t *testing.T) {
	for _, f := range evaluate(t, goodHost()) {
		if f.Status != doctor.Pass {
			t.Errorf("%s: %s %s (fix: %s)", f.Check, f.Status, f.Message, f.Fix)
		}
	}
}

func TestSystemPythonOlderThanTheProjectNeedsIsFailedAndTheVenvOffered(t *testing.T) {
	h := goodHost()
	h.Versions["/usr/bin/python3"] = "Python 3.11.2"
	h.Files[root+"/.venv/bin/python3"] = ""
	h.Versions[root+"/.venv/bin/python3"] = "Python 3.12.3"
	f := find(t, evaluate(t, h), "python3")
	if f.Status != doctor.Fail || !strings.Contains(f.Message, "3.11.2") {
		t.Fatalf("got %+v", f)
	}
	want := doctor.Export{Name: "PATH", Value: root + "/.venv/bin", Prepend: true}
	if len(f.Exports) != 1 || f.Exports[0] != want {
		t.Fatalf("exports %v, want %v", f.Exports, want)
	}
}

func TestPythonWithNoVenvIsRepairedByTheConfiguredCommand(t *testing.T) {
	h := goodHost()
	h.Versions["/usr/bin/python3"] = "Python 3.11.2"
	f := find(t, evaluate(t, h), "python3")
	if f.Status != doctor.Fail || len(f.Actions) != 1 || strings.Join(f.Actions[0].Argv, " ") != "uv sync" || f.Actions[0].Dir != root {
		t.Fatalf("got %+v", f)
	}
}

func TestOnlyTheFirstCopyOnPathCounts(t *testing.T) {
	h := goodHost()
	h.Env["PATH"] = "/old/bin:/opt/bun/bin"
	h.Files["/old/bin/bun"] = ""
	h.Versions["/old/bin/bun"] = "1.3.8"
	f := find(t, evaluate(t, h), "bun")
	want := doctor.Export{Name: "PATH", Value: "/opt/bun/bin", Prepend: true}
	if f.Status != doctor.Fail || len(f.Exports) != 1 || f.Exports[0] != want {
		t.Fatalf("pinned bun is later on PATH: got %+v", f)
	}
	if !strings.Contains(f.Message, "exactly 1.4.2") || !strings.Contains(f.Message, "1.3.8") {
		t.Fatalf("message should name the pin and the wrong version: %s", f.Message)
	}
}

func TestAMissingPinnedProgramGetsItsHint(t *testing.T) {
	h := goodHost()
	delete(h.Files, "/opt/bun/bin/bun")
	f := find(t, evaluate(t, h), "bun")
	if f.Status != doctor.Fail || !strings.Contains(f.Fix, "install the pinned bun") {
		t.Fatalf("got %+v", f)
	}
}

func TestAnUnreadablePinIsAFailureNotAPass(t *testing.T) {
	h := goodHost()
	delete(h.Files, root+"/pin.py")
	f := find(t, evaluate(t, h), "bun")
	if f.Status != doctor.Fail || !strings.Contains(f.Message, "pin.py") {
		t.Fatalf("got %+v", f)
	}
	h.Files[root+"/pin.py"] = "nothing here"
	if f := find(t, evaluate(t, h), "bun"); f.Status != doctor.Fail || !strings.Contains(f.Message, "no line matching") {
		t.Fatalf("got %+v", f)
	}
}

func TestMissingBufIsRepairedAndAVenvBufNeedsPath(t *testing.T) {
	h := goodHost()
	delete(h.Files, "/usr/bin/buf")
	f := find(t, evaluate(t, h), "buf")
	if f.Status != doctor.Fail || len(f.Actions) != 1 || strings.Join(f.Actions[0].Argv, " ") != "uv sync --group proto" {
		t.Fatalf("got %+v", f)
	}
	h.Files[root+"/.venv/bin/buf"] = ""
	f = find(t, evaluate(t, h), "buf")
	if f.Status != doctor.Fail || len(f.Exports) != 1 || f.Exports[0].Value != root+"/.venv/bin" {
		t.Fatalf("installed in the venv but not on PATH: got %+v", f)
	}
}

func TestAHalfInstalledTreeIsRemovedThenInstalled(t *testing.T) {
	h := goodHost()
	delete(h.Files, root+"/clients/node_modules/.modules.yaml")
	h.Files[root+"/clients/node_modules/left-pad/index.js"] = ""
	f := find(t, evaluate(t, h), "deps")
	if f.Status != doctor.Fail || len(f.Actions) != 2 {
		t.Fatalf("got %+v", f)
	}
	if f.Actions[0].Kind != doctor.ActionRemoveTree || f.Actions[0].Path != root+"/clients/node_modules" {
		t.Fatalf("first action: %+v", f.Actions[0])
	}
	if f.Actions[1].Kind != doctor.ActionRun || f.Actions[1].Dir != root+"/clients" {
		t.Fatalf("second action: %+v", f.Actions[1])
	}
}

func TestAnAbsentTreeIsOnlyAWarning(t *testing.T) {
	h := goodHost()
	delete(h.Files, root+"/clients/node_modules/.modules.yaml")
	f := find(t, evaluate(t, h), "deps")
	if f.Status != doctor.Warn || len(f.Actions) != 1 {
		t.Fatalf("got %+v", f)
	}
}

func TestGoflagsIsRequiredOnlyInLinkedWorktrees(t *testing.T) {
	h := goodHost()
	h.Env["GOFLAGS"] = "-mod=mod"
	f := find(t, evaluate(t, h), "GOFLAGS")
	if f.Status != doctor.Fail || f.Exports[0].Value != "-mod=mod -buildvcs=false" || f.Exports[0].Prepend {
		t.Fatalf("got %+v", f)
	}
	h.Linked = false
	if f := find(t, evaluate(t, h), "GOFLAGS"); f.Status != doctor.Pass {
		t.Fatalf("a plain clone needs no flag: %+v", f)
	}
}

func TestTempOnANetworkFilesystemOffersALocalCandidate(t *testing.T) {
	h := goodHost()
	h.Env["TMPDIR"] = ""
	h.Env["LOCAL_TMP"] = "/scratch/t"
	h.Mounts["/tmp"] = doctor.Filesystem{Type: "nfs4", Device: 7}
	f := find(t, evaluate(t, h), "tmpdir")
	if f.Status != doctor.Warn || !strings.Contains(f.Message, "network file system") {
		t.Fatalf("got %+v", f)
	}
	if len(f.Exports) != 1 || f.Exports[0] != (doctor.Export{Name: "TMPDIR", Value: "/scratch/t"}) {
		t.Fatalf("exports %v", f.Exports)
	}
	if len(f.Actions) != 1 || f.Actions[0].Kind != doctor.ActionMakeDirectory {
		t.Fatalf("actions %v", f.Actions)
	}
}

func TestTempOnAnotherFilesystemThanTheRepositoryIsWarned(t *testing.T) {
	h := goodHost()
	h.Mounts["/scratch"] = doctor.Filesystem{Type: "tmpfs", Device: 9}
	h.Env["USER"] = "dev"
	f := find(t, evaluate(t, h), "tmpdir")
	if f.Status != doctor.Warn || !strings.Contains(f.Message, "different file systems") {
		t.Fatalf("got %+v", f)
	}
	// The only candidate is /var/tmp/repo-dev, on the repository's own file system.
	if len(f.Exports) != 1 || f.Exports[0].Value != "/var/tmp/repo-dev" {
		t.Fatalf("exports %v", f.Exports)
	}
}

func TestACandidateThatLeavesTheProblemInPlaceIsNotOffered(t *testing.T) {
	h := goodHost()
	h.Mounts["/scratch"] = doctor.Filesystem{Type: "tmpfs", Device: 9}
	h.Mounts["/other"] = doctor.Filesystem{Type: "tmpfs", Device: 10}
	h.Env["LOCAL_TMP"] = "/other/t" // local, but not the repository's file system
	h.Env["USER"] = ""              // skips the second candidate
	f := find(t, evaluate(t, h), "tmpdir")
	if f.Status != doctor.Warn || len(f.Exports) != 0 || !strings.Contains(f.Fix, "LOCAL_TMP") {
		t.Fatalf("got %+v", f)
	}
}

func TestANetworkCandidateIsNeverOffered(t *testing.T) {
	h := goodHost()
	h.Mounts["/tmp"] = doctor.Filesystem{Type: "nfs", Device: 7}
	h.Mounts["/nfs"] = doctor.Filesystem{Type: "nfs4", Device: 7}
	h.Env["TMPDIR"] = "/tmp/x"
	h.Env["LOCAL_TMP"] = "/nfs/t"
	f := find(t, evaluate(t, h), "tmpdir")
	if len(f.Exports) != 0 {
		t.Fatalf("offered a network directory: %+v", f)
	}
}

func TestALongTempPathIsWarned(t *testing.T) {
	h := goodHost()
	h.Env["TMPDIR"] = "/scratch/" + strings.Repeat("d", 40)
	f := find(t, evaluate(t, h), "tmpdir")
	if f.Status != doctor.Warn || !strings.Contains(f.Message, "socket path limit") {
		t.Fatalf("got %+v", f)
	}
}

func TestGatePlanErrorAndHookStates(t *testing.T) {
	h := goodHost()
	h.Plan = errors.New("unowned changed paths: cli/check.sh")
	if f := find(t, evaluate(t, h), "gate-plan"); f.Status != doctor.Fail || !strings.Contains(f.Message, "cli/check.sh") {
		t.Fatalf("got %+v", f)
	}
	delete(h.Config, "core.hooksPath")
	f := find(t, evaluate(t, h), "hooks")
	if f.Status != doctor.Fail || f.Actions[0].Key != "core.hooksPath" || f.Actions[0].Value != ".githooks" {
		t.Fatalf("unset hooks: %+v", f)
	}
	h.Config["core.hooksPath"] = "/home/me/hooks"
	f = find(t, evaluate(t, h), "hooks")
	if f.Status != doctor.Warn || len(f.Actions) != 0 {
		t.Fatalf("a foreign hooks directory is the user's to change: %+v", f)
	}
}

func TestAnInstructionNamingAMissingScriptFails(t *testing.T) {
	h := goodHost()
	delete(h.Files, root+"/scripts/check.sh")
	f := find(t, evaluate(t, h), "instructions")
	if f.Status != doctor.Fail || !strings.Contains(f.Message, "scripts/check.sh") {
		t.Fatalf("got %+v", f)
	}
}

func TestExtractReferences(t *testing.T) {
	text := "Run `./scripts/check_format.sh`, then scripts/a.py. See docs/scripts/x.sh, `scripts/*.sh`, " +
		"`scripts/`, support/repoctl/repoctl, and .githooks/pre-push. Again scripts/a.py."
	got := doctor.ExtractReferences(text, []string{"scripts/", "support/", ".githooks/"})
	want := []string{"scripts/check_format.sh", "scripts/a.py", "support/repoctl/repoctl", ".githooks/pre-push"}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}

func TestRunReportsAndFailsOnlyOnFailures(t *testing.T) {
	cfg := config(t)
	h := goodHost()
	var out bytes.Buffer
	if err := doctor.Run(cfg, root, h, doctor.Options{}, &out); err != nil {
		t.Fatalf("a good machine must exit zero: %v\n%s", err, out.String())
	}
	h.Mounts["/scratch"] = doctor.Filesystem{Type: "tmpfs", Device: 9}
	out.Reset()
	if err := doctor.Run(cfg, root, h, doctor.Options{}, &out); err != nil {
		t.Fatalf("a warning must not fail: %v", err)
	}
	h.Env["GOFLAGS"] = ""
	out.Reset()
	err := doctor.Run(cfg, root, h, doctor.Options{}, &out)
	if err == nil || !strings.Contains(err.Error(), "1 check(s) failed") {
		t.Fatalf("got %v", err)
	}
	for _, want := range []string{"FAIL GOFLAGS", "fix: export GOFLAGS='-buildvcs=false'", "doctor --env"} {
		if !strings.Contains(out.String(), want) {
			t.Errorf("report lacks %q:\n%s", want, out.String())
		}
	}
}

func TestEnvPrintsOnlyExports(t *testing.T) {
	cfg := config(t)
	h := goodHost()
	h.Env["GOFLAGS"] = ""
	h.Env["PATH"] = "/old/bin:/opt/bun/bin"
	h.Files["/old/bin/bun"] = ""
	h.Versions["/old/bin/bun"] = "1.3.8"
	var out bytes.Buffer
	if err := doctor.Run(cfg, root, h, doctor.Options{Env: true}, &out); err != nil {
		t.Fatal(err)
	}
	want := "export PATH='/opt/bun/bin'\"${PATH:+:$PATH}\"\nexport GOFLAGS='-buildvcs=false'\n"
	if out.String() != want {
		t.Fatalf("got %q, want %q", out.String(), want)
	}
}

func TestFixRepairsAHalfInstalledTreeAndChecksAgain(t *testing.T) {
	cfg := config(t)
	h := goodHost()
	delete(h.Files, root+"/clients/node_modules/.modules.yaml")
	h.Files[root+"/clients/node_modules/left-pad/index.js"] = ""
	h.OnExecute = func(h *doctor.FakeHost, a doctor.Action) {
		if a.Kind == doctor.ActionRun && a.Argv[0] == "pnpm" {
			h.Files[root+"/clients/node_modules/.modules.yaml"] = ""
		}
	}
	var out bytes.Buffer
	if err := doctor.Run(cfg, root, h, doctor.Options{Fix: true}, &out); err != nil {
		t.Fatalf("%v\n%s", err, out.String())
	}
	if len(h.Executed) != 2 || h.Executed[0].Kind != doctor.ActionRemoveTree || h.Executed[1].Argv[0] != "pnpm" {
		t.Fatalf("executed %+v", h.Executed)
	}
	if _, ok := h.Files[root+"/clients/node_modules/left-pad/index.js"]; ok {
		t.Fatal("the half-installed tree was left in place")
	}
}

func TestFixEnablesTheHookAndLeavesAForeignOneAlone(t *testing.T) {
	cfg := config(t)
	h := goodHost()
	delete(h.Config, "core.hooksPath")
	var out bytes.Buffer
	if err := doctor.Run(cfg, root, h, doctor.Options{Fix: true}, &out); err != nil {
		t.Fatal(err)
	}
	if h.Config["core.hooksPath"] != ".githooks" {
		t.Fatalf("hooks path %q", h.Config["core.hooksPath"])
	}
	h.Config["core.hooksPath"] = "/home/me/hooks"
	h.Executed = nil
	if err := doctor.Run(cfg, root, h, doctor.Options{Fix: true}, &out); err != nil {
		t.Fatal(err)
	}
	if len(h.Executed) != 0 || h.Config["core.hooksPath"] != "/home/me/hooks" {
		t.Fatalf("touched a foreign hooks path: %+v", h.Executed)
	}
}

func TestFixStopsAtAFailedRepairAndSaysWhich(t *testing.T) {
	cfg := config(t)
	h := goodHost()
	delete(h.Files, "/usr/bin/buf")
	h.RunFails = errors.New("network down")
	var out bytes.Buffer
	err := doctor.Run(cfg, root, h, doctor.Options{Fix: true}, &out)
	if err == nil || !strings.Contains(err.Error(), "repair buf") || !strings.Contains(err.Error(), "network down") {
		t.Fatalf("got %v", err)
	}
}

func TestApplyRefusesToDeleteOutsideTheRepository(t *testing.T) {
	for _, path := range []string{"/", "/work", root, root + "/..", root + "/../other", "/etc", "relative/dir"} {
		h := goodHost()
		fd := doctor.Finding{Check: "x", Status: doctor.Fail, Actions: []doctor.Action{{Kind: doctor.ActionRemoveTree, Path: path}}}
		if _, err := doctor.Apply(h, root, []doctor.Finding{fd}, &bytes.Buffer{}); err == nil {
			t.Errorf("deleted %q", path)
		}
		if len(h.Executed) != 0 {
			t.Errorf("executed %+v for %q", h.Executed, path)
		}
	}
	h := goodHost()
	fd := doctor.Finding{Check: "x", Status: doctor.Fail, Actions: []doctor.Action{{Kind: doctor.ActionRemoveTree, Path: root + "/clients/node_modules"}}}
	if _, err := doctor.Apply(h, root, []doctor.Finding{fd}, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
}

// randomHost builds a machine from a seed: every ingredient of the checks varies independently.
func randomHost(seed int64) *doctor.FakeHost {
	r := rand.New(rand.NewSource(seed))
	pick := func(options ...string) string { return options[r.Intn(len(options))] }
	h := goodHost()
	h.Linked = r.Intn(2) == 0
	h.Env["GOFLAGS"] = pick("", "-mod=mod", "-buildvcs=false", "-mod=mod -buildvcs=false")
	h.Env["TMPDIR"] = pick("", "/tmp/x", "/scratch/tmp", "/scratch/"+strings.Repeat("d", 50))
	h.Env["LOCAL_TMP"] = pick("", "/scratch/t", "/other/t", "/nfs/t")
	h.Env["USER"] = pick("", "dev")
	h.Mounts["/tmp"] = doctor.Filesystem{Type: pick("ext4", "nfs4", "tmpfs"), Device: uint64(2 + r.Intn(3))}
	h.Mounts["/other"] = doctor.Filesystem{Type: pick("ext4", "tmpfs"), Device: uint64(5 + r.Intn(2))}
	h.Mounts["/nfs"] = doctor.Filesystem{Type: "nfs", Device: 8}
	h.Mounts["/var"] = doctor.Filesystem{Type: pick("ext4", "tmpfs"), Device: uint64(1 + r.Intn(2))}
	h.Env["PATH"] = pick("/usr/bin:/opt/bun/bin", "/old/bin:/usr/bin:/opt/bun/bin", "/usr/bin")
	h.Files["/old/bin/bun"] = ""
	h.Versions["/old/bin/bun"] = pick("1.3.8", "1.4.2")
	h.Files["/old/bin/python3"] = ""
	h.Versions["/old/bin/python3"] = pick("Python 3.11.2", "Python 3.13.0")
	h.Versions["/usr/bin/python3"] = pick("Python 3.11.2", "Python 3.12.3")
	if r.Intn(2) == 0 {
		h.Files[root+"/.venv/bin/python3"] = ""
		h.Versions[root+"/.venv/bin/python3"] = pick("Python 3.12.3", "Python 3.11.9")
	}
	if r.Intn(2) == 0 {
		delete(h.Files, "/usr/bin/buf")
		if r.Intn(2) == 0 {
			h.Files[root+"/.venv/bin/buf"] = ""
		}
	}
	if r.Intn(2) == 0 {
		delete(h.Files, "/opt/bun/bin/bun")
	}
	switch r.Intn(3) {
	case 0:
		delete(h.Files, root+"/clients/node_modules/.modules.yaml")
	case 1:
		delete(h.Files, root+"/clients/node_modules/.modules.yaml")
		h.Files[root+"/clients/node_modules/x"] = ""
	}
	if r.Intn(2) == 0 {
		delete(h.Config, "core.hooksPath")
	}
	if r.Intn(4) == 0 {
		delete(h.Files, root+"/scripts/check.sh")
	}
	if r.Intn(4) == 0 {
		h.Plan = errors.New("unowned")
	}
	return h
}

func TestEveryProblemNamesItsFix(t *testing.T) {
	cfg := config(t)
	err := quick.Check(func(seed int64) bool {
		for _, f := range doctor.Evaluate(cfg, doctor.Gather(cfg, root, randomHost(seed))) {
			if f.Status != doctor.Pass && (f.Fix == "" || f.Message == "") {
				t.Logf("seed %d: %+v", seed, f)
				return false
			}
			if f.Status == doctor.Pass && (len(f.Actions) > 0 || len(f.Exports) > 0 || f.Fix != "") {
				t.Logf("seed %d: a pass carries a fix: %+v", seed, f)
				return false
			}
		}
		return true
	}, &quick.Config{MaxCount: 500})
	if err != nil {
		t.Fatal(err)
	}
}

func TestEvaluatingTwiceGivesTheSameFindings(t *testing.T) {
	cfg := config(t)
	err := quick.Check(func(seed int64) bool {
		facts := doctor.Gather(cfg, root, randomHost(seed))
		return fmt.Sprintf("%+v", doctor.Evaluate(cfg, facts)) == fmt.Sprintf("%+v", doctor.Evaluate(cfg, facts))
	}, &quick.Config{MaxCount: 200})
	if err != nil {
		t.Fatal(err)
	}
}

// applyExports does what `eval "$(repoctl doctor --env)"` does to a shell.
func applyExports(env map[string]string, exports []doctor.Export) {
	for _, e := range exports {
		if e.Prepend && env[e.Name] != "" {
			env[e.Name] = e.Value + ":" + env[e.Name]
		} else {
			env[e.Name] = e.Value
		}
	}
}

func TestEvaluatingEnvExportsResolvesEveryExportFinding(t *testing.T) {
	cfg := config(t)
	err := quick.Check(func(seed int64) bool {
		h := randomHost(seed)
		before := doctor.Evaluate(cfg, doctor.Gather(cfg, root, h))
		applyExports(h.Env, doctor.Exports(before))
		after := doctor.Evaluate(cfg, doctor.Gather(cfg, root, h))
		if left := doctor.Exports(after); len(left) != 0 {
			t.Logf("seed %d: exports remain after applying them: %v", seed, left)
			return false
		}
		return doctor.Failed(after) <= doctor.Failed(before)
	}, &quick.Config{MaxCount: 500})
	if err != nil {
		t.Fatal(err)
	}
}

func TestFixNeverLeavesMoreFailuresThanItFound(t *testing.T) {
	cfg := config(t)
	err := quick.Check(func(seed int64) bool {
		h := randomHost(seed)
		h.OnExecute = func(h *doctor.FakeHost, a doctor.Action) {
			switch {
			case a.Kind == doctor.ActionRun && a.Argv[0] == "pnpm":
				h.Files[root+"/clients/node_modules/.modules.yaml"] = ""
			case a.Kind == doctor.ActionRun && a.Argv[0] == "uv":
				h.Files[root+"/.venv/bin/python3"] = ""
				h.Versions[root+"/.venv/bin/python3"] = "Python 3.12.3"
				h.Files[root+"/.venv/bin/buf"] = ""
			}
		}
		before := doctor.Failed(doctor.Evaluate(cfg, doctor.Gather(cfg, root, h)))
		_ = doctor.Run(cfg, root, h, doctor.Options{Fix: true}, &bytes.Buffer{})
		after := doctor.Failed(doctor.Evaluate(cfg, doctor.Gather(cfg, root, h)))
		return after <= before
	}, &quick.Config{MaxCount: 300})
	if err != nil {
		t.Fatal(err)
	}
}

func TestAFixDirectoryThatWouldHideAnotherProgramsPinIsNotOffered(t *testing.T) {
	h := goodHost()
	delete(h.Files, "/usr/bin/buf")
	h.Files[root+"/.venv/bin/buf"] = ""
	h.Files[root+"/.venv/bin/python3"] = ""
	h.Versions[root+"/.venv/bin/python3"] = "Python 3.11.9" // too old: first on PATH would break python3
	f := find(t, evaluate(t, h), "buf")
	if f.Status != doctor.Fail || len(f.Exports) != 0 || !strings.Contains(f.Message, "python3") {
		t.Fatalf("got %+v", f)
	}
}
