package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

func rootPath() (string, error) {
	if root := os.Getenv("CI_IMPACT_ROOT"); root != "" {
		return root, nil
	}
	wd, err := os.Getwd()
	if err != nil {
		return "", err
	}
	for dir := wd; ; dir = filepath.Dir(dir) {
		if _, err := os.Stat(filepath.Join(dir, "ci-components.toml")); err == nil {
			return dir, nil
		}
		if filepath.Dir(dir) == dir {
			return "", fmt.Errorf("cannot locate ci-components.toml")
		}
	}
}
func emit(p plan, asJSON bool) error {
	if asJSON {
		out, err := json.MarshalIndent(p, "", "  ")
		if err != nil {
			return err
		}
		fmt.Println(string(out))
		return nil
	}
	selected := []string{}
	for _, job := range jobs {
		if p.Jobs[job] {
			selected = append(selected, job)
		}
	}
	fmt.Printf("Changed paths: %d\n", len(p.ChangedPaths))
	line := "none"
	if len(selected) > 0 {
		line = strings.Join(selected, ", ")
	}
	fmt.Println("Selected jobs: " + line)
	for _, job := range selected {
		for _, reason := range p.JobReasons[job] {
			fmt.Printf("  %s: %s\n", job, reason)
		}
	}
	if len(p.NativeTargets) > 0 {
		fmt.Println("Native targets: " + strings.Join(p.NativeTargets, ", "))
	}
	if len(p.NativeLanguages) > 0 {
		fmt.Println("Native languages: " + strings.Join(p.NativeLanguages, ", "))
	}
	if len(p.PnpmPackages) > 0 {
		fmt.Println("pnpm packages: " + strings.Join(p.PnpmPackages, ", "))
	}
	return nil
}
func writeOutputs(path string, p plan) error {
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	defer f.Close()
	for _, job := range jobs {
		fmt.Fprintf(f, "%s=%t\n", job, p.Jobs[job])
	}
	for _, key := range []struct {
		name  string
		value []string
	}{{"native_targets", p.NativeTargets}, {"native_languages", p.NativeLanguages}, {"pnpm_packages", p.PnpmPackages}} {
		data, _ := json.Marshal(key.value)
		fmt.Fprintf(f, "%s=%s\n", key.name, data)
	}
	return nil
}
func cli(args []string) error {
	if len(args) == 0 {
		return fmt.Errorf("usage: ci-impact-go {plan|explain|validate}")
	}
	root, err := rootPath()
	if err != nil {
		return err
	}
	g, err := readPolicy(root)
	if err != nil {
		return err
	}
	switch args[0] {
	case "validate":
		if len(args) != 1 {
			return fmt.Errorf("validate takes no arguments")
		}
		paths, err := trackedPaths(root)
		if err != nil {
			return err
		}
		if _, err := g.selectPaths(paths); err != nil {
			return err
		}
		fmt.Printf("Valid graph: %d components; all tracked paths classified.\n", len(g.Components))
		return nil
	case "explain":
		// Accept `explain PATH --json`, matching the Python CLI.
		commandArgs := make([]string, 0, len(args)-1)
		for _, arg := range args[1:] {
			if arg != "--json" {
				commandArgs = append(commandArgs, arg)
			}
		}
		fs := flag.NewFlagSet("explain", flag.ContinueOnError)
		asJSON := fs.Bool("json", false, "Print JSON plan")
		if err := fs.Parse(commandArgs); err != nil {
			return err
		}
		if len(commandArgs) != len(args)-1 {
			*asJSON = true
		}
		if fs.NArg() != 1 {
			return fmt.Errorf("explain requires one path")
		}
		p, err := g.selectPaths([]string{fs.Arg(0)})
		if err != nil {
			return err
		}
		return emit(p, *asJSON)
	case "plan":
		fs := flag.NewFlagSet("plan", flag.ContinueOnError)
		base := fs.String("base", "main", "Base revision")
		head := fs.String("head", "HEAD", "Head revision")
		event := fs.String("event", "pull_request", "Event type")
		asJSON := fs.Bool("json", false, "Print JSON plan")
		output := fs.String("github-output", "", "Append GitHub outputs")
		if err := fs.Parse(args[1:]); err != nil {
			return err
		}
		if fs.NArg() != 0 {
			return fmt.Errorf("unexpected plan arguments")
		}
		paths, err := changedPaths(root, *base, *head, *event)
		if err != nil {
			return err
		}
		p, err := g.selectPaths(paths)
		if err != nil {
			return err
		}
		if err := emit(p, *asJSON); err != nil {
			return err
		}
		if *output != "" {
			return writeOutputs(*output, p)
		}
		return nil
	default:
		return fmt.Errorf("unknown command %q", args[0])
	}
}
func main() {
	if err := cli(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "CI impact failed:", err)
		os.Exit(2)
	}
}
