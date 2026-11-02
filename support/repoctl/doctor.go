package main

import (
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"repoctl/internal/doctor"
	"repoctl/internal/gitrepo"
)

// runDoctor is `repoctl doctor`: see internal/doctor.
func runDoctor(root, configPath string, g graph, args []string, out io.Writer) error {
	cfg, err := doctor.ReadConfig(policyDirectory(root, configPath))
	if err != nil {
		return err
	}
	host := doctor.OSHost{Root: root, Plan: gatePlanCheck(root, g), Stdout: os.Stderr, Stderr: os.Stderr}
	return runDoctorWith(cfg, root, host, args, out)
}

// runDoctorWith parses the doctor's flags and runs it on a Host.
func runDoctorWith(cfg doctor.Config, root string, host doctor.Host, args []string, out io.Writer) error {
	fs := flag.NewFlagSet("doctor", flag.ContinueOnError)
	fix := fs.Bool("fix", false, "perform the safe repairs (install what is missing, enable the gate hook, create a local TMPDIR), then check again")
	env := fs.Bool("env", false, "print only the environment exports a shell should evaluate: eval \"$(repoctl doctor --env)\"")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 0 {
		return fmt.Errorf("unexpected doctor arguments")
	}
	if *fix && *env {
		return fmt.Errorf("--fix and --env cannot be combined: repair first, then print the exports")
	}
	return doctor.Run(cfg, root, host, doctor.Options{Fix: *fix, Env: *env}, out)
}

func policyDirectory(root, configPath string) string {
	if filepath.IsAbs(configPath) {
		return configPath
	}
	return filepath.Join(root, configPath)
}

// gatePlanCheck returns the probe that says whether the gate can plan the working tree's
// changes: whether every changed path has an owner. It classifies the paths against the current
// policy only. The gate's own planning also reads the policy at the base revision, which takes
// tens of seconds on a network file system, too slow for a check; an unowned path is the failure
// that planning reports for a change the policy does not know.
func gatePlanCheck(root string, g graph) func() error {
	return func() error {
		changes, err := gitrepo.WorktreeChanges(root)
		if err != nil {
			return err
		}
		var paths []string
		for _, c := range changes {
			for _, p := range []string{c.Path, c.OldPath, c.NewPath} {
				if p != "" {
					paths = append(paths, p)
				}
			}
		}
		_, err = g.forLocalTest().selectPaths(paths)
		return err
	}
}

// gateSetupHint is what a refused push adds: the names of the failing setup checks, when there
// are any, and the command that fixes them. It is empty when the machine is in order or the
// repository has no doctor policy.
func gateSetupHint(root, configPath string, g graph) string {
	cfg, err := doctor.ReadConfig(policyDirectory(root, configPath))
	if err != nil {
		return ""
	}
	host := doctor.OSHost{Root: root, Plan: gatePlanCheck(root, g)}
	return setupHint(doctor.Evaluate(cfg, doctor.Gather(cfg, root, host)))
}

func setupHint(findings []doctor.Finding) string {
	var names []string
	for _, f := range findings {
		if f.Status != doctor.Pass {
			names = append(names, f.Check)
		}
	}
	if len(names) == 0 {
		return ""
	}
	return fmt.Sprintf("gate: this machine's setup may explain the failure (%s). Run ./support/repoctl/repoctl doctor for the exact fixes, or doctor --fix to apply the safe ones.", strings.Join(names, ", "))
}
