package main

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"repoctl/internal/doctor"
)

const doctorTestRoot = "/work/repo"

func doctorTestConfig(t *testing.T) doctor.Config {
	t.Helper()
	cfg, err := doctor.ParseConfig("test", `
[hooks]
path = ".githooks"
[temp]
max_path_length = 64
[[env_flags]]
name = "GOFLAGS"
flag = "-buildvcs=false"
linked_worktree_only = true
`)
	if err != nil {
		t.Fatal(err)
	}
	return cfg
}

func doctorTestHost() *doctor.FakeHost {
	h := doctor.NewFakeHost()
	h.Env["TMPDIR"] = "/scratch"
	h.Mounts["/"] = doctor.Filesystem{Type: "ext4", Device: 1}
	h.Config["core.hooksPath"] = ".githooks"
	h.Linked = true
	return h
}

// The policy this repository ships must parse, or `repoctl doctor` is broken for everyone.
func TestTheRepositorysDoctorPolicyParses(t *testing.T) {
	dir := filepath.Join("..", "..", ".repoctl")
	if _, err := os.Stat(filepath.Join(dir, doctor.ConfigFile)); err != nil {
		t.Skip("not in a full checkout")
	}
	if _, err := doctor.ReadConfig(dir); err != nil {
		t.Fatal(err)
	}
}

func TestDoctorCommandFlags(t *testing.T) {
	cfg := doctorTestConfig(t)
	for name, args := range map[string][]string{
		"fix with env": {"--fix", "--env"},
		"positional":   {"extra"},
		"unknown flag": {"--nope"},
	} {
		if err := runDoctorWith(cfg, doctorTestRoot, doctorTestHost(), args, &bytes.Buffer{}); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
}

func TestDoctorEnvPrintsTheExportsAFailingMachineNeeds(t *testing.T) {
	cfg := doctorTestConfig(t)
	var out bytes.Buffer
	if err := runDoctorWith(cfg, doctorTestRoot, doctorTestHost(), []string{"--env"}, &out); err != nil {
		t.Fatal(err)
	}
	if out.String() != "export GOFLAGS='-buildvcs=false'\n" {
		t.Fatalf("got %q", out.String())
	}
}

func TestDoctorExitsNonZeroOnAFailureAndZeroOtherwise(t *testing.T) {
	cfg := doctorTestConfig(t)
	h := doctorTestHost()
	if err := runDoctorWith(cfg, doctorTestRoot, h, nil, &bytes.Buffer{}); err == nil {
		t.Fatal("a worktree without GOFLAGS passed")
	}
	h.Env["GOFLAGS"] = "-buildvcs=false"
	if err := runDoctorWith(cfg, doctorTestRoot, h, nil, &bytes.Buffer{}); err != nil {
		t.Fatal(err)
	}
}

func TestSetupHintNamesTheProblemsAndIsSilentWhenThereAreNone(t *testing.T) {
	cfg := doctorTestConfig(t)
	h := doctorTestHost()
	findings := doctor.Evaluate(cfg, doctor.Gather(cfg, doctorTestRoot, h))
	hint := setupHint(findings)
	if !strings.Contains(hint, "GOFLAGS") || !strings.Contains(hint, "repoctl doctor") {
		t.Fatalf("hint = %q", hint)
	}
	h.Env["GOFLAGS"] = "-buildvcs=false"
	if hint := setupHint(doctor.Evaluate(cfg, doctor.Gather(cfg, doctorTestRoot, h))); hint != "" {
		t.Fatalf("a machine in order got a hint: %q", hint)
	}
}

func TestARefusedPushPointsAtTheDoctorAndAPassingOneDoesNot(t *testing.T) {
	hint := func() string { return "gate: run the doctor" }
	var out bytes.Buffer
	refusal := errors.New("pre-push gate failed: types")
	if err := withSetupHint(&out, refusal, hint); err != refusal {
		t.Fatalf("the refusal changed: %v", err)
	}
	if !strings.Contains(out.String(), "run the doctor") {
		t.Fatalf("no hint: %q", out.String())
	}
	out.Reset()
	if err := withSetupHint(&out, nil, hint); err != nil || out.Len() != 0 {
		t.Fatalf("a passing gate printed %q (%v)", out.String(), err)
	}
	if err := withSetupHint(&out, refusal, nil); err != refusal || out.Len() != 0 {
		t.Fatalf("no hint source printed %q", out.String())
	}
}
