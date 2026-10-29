package main

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"repoctl/internal/devlaunch"
)

// runDev is `repoctl dev`: see internal/devlaunch.
func runDev(root, configPath string, args []string) error {
	fs := flag.NewFlagSet("dev", flag.ContinueOnError)
	machine := fs.String("machine", "", "ssh alias of a machine to ship the committed HEAD to before opening the app")
	checkout := fs.String("checkout", "", "the VibeSys checkout on the machine (default: the one the desktop app saved for the machine)")
	dryRun := fs.Bool("dry-run", false, "print the plan and change nothing")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 0 {
		return fmt.Errorf("unexpected dev arguments")
	}
	policyDir := configPath
	if !filepath.IsAbs(policyDir) {
		policyDir = filepath.Join(root, policyDir)
	}
	cfg, err := devlaunch.ReadConfig(policyDir)
	if err != nil {
		return err
	}
	env := devlaunch.Env{
		Root:   root,
		Runner: devlaunch.OSRunner{Root: root},
		Clock:  devlaunch.RealClock{},
		Files:  devlaunch.OSFiles{Root: root},
		Out:    os.Stdout,
	}
	return devlaunch.Run(env, cfg, devlaunch.Options{Machine: *machine, Checkout: *checkout, DryRun: *dryRun})
}
