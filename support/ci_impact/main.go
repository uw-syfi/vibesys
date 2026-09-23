package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

func rootPath(configPath string) (string, error) {
	if filepath.IsAbs(configPath) {
		if _, err := os.Stat(configPath); err != nil {
			return "", fmt.Errorf("configuration %q: %w", configPath, err)
		}
		return filepath.Dir(configPath), nil
	}
	if root := os.Getenv("CI_IMPACT_ROOT"); root != "" {
		return root, nil
	}
	wd, err := os.Getwd()
	if err != nil {
		return "", err
	}
	for dir := wd; ; dir = filepath.Dir(dir) {
		candidate := configPath
		if !filepath.IsAbs(candidate) {
			candidate = filepath.Join(dir, candidate)
		}
		if _, err := os.Stat(candidate); err == nil {
			return dir, nil
		}
		if filepath.Dir(dir) == dir {
			return "", fmt.Errorf("cannot locate configuration %q", configPath)
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
	for job := range p.Jobs {
		if p.Jobs[job] {
			selected = append(selected, job)
		}
	}
	sort.Strings(selected)
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
	collectionNames := make([]string, 0, len(p.Collections))
	for name := range p.Collections {
		collectionNames = append(collectionNames, name)
	}
	sort.Strings(collectionNames)
	for _, name := range collectionNames {
		values := p.Collections[name]
		if len(values) > 0 {
			fmt.Printf("%s: %s\n", name, strings.Join(values, ", "))
		}
	}
	return nil
}
func writeOutputs(path string, p plan) error {
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	defer f.Close()
	jobNames := make([]string, 0, len(p.Jobs))
	for job := range p.Jobs {
		jobNames = append(jobNames, job)
	}
	sort.Strings(jobNames)
	for _, job := range jobNames {
		selected := p.Jobs[job]
		if _, err := fmt.Fprintf(f, "%s=%t\n", job, selected); err != nil {
			return err
		}
	}
	collectionNames := make([]string, 0, len(p.Collections))
	for name := range p.Collections {
		collectionNames = append(collectionNames, name)
	}
	sort.Strings(collectionNames)
	for _, name := range collectionNames {
		values := p.Collections[name]
		data, err := json.Marshal(values)
		if err != nil {
			return err
		}
		if _, err := fmt.Fprintf(f, "%s=%s\n", name, data); err != nil {
			return err
		}
	}
	return nil
}
func cli(args []string) error {
	configPath := "ci-impact.toml"
	filtered := make([]string, 0, len(args))
	for i := 0; i < len(args); i++ {
		if args[i] == "--config" {
			if i+1 >= len(args) {
				return fmt.Errorf("--config requires a path")
			}
			i++
			configPath = args[i]
			continue
		}
		if strings.HasPrefix(args[i], "--config=") {
			configPath = strings.TrimPrefix(args[i], "--config=")
			continue
		}
		filtered = append(filtered, args[i])
	}
	args = filtered
	if len(args) == 0 {
		return fmt.Errorf("usage: ci-impact {plan|explain|validate|run-native}")
	}
	root, err := rootPath(configPath)
	if err != nil {
		return err
	}
	g, err := readPolicy(root, configPath)
	if err != nil {
		return err
	}
	switch args[0] {
	case "run-native":
		fs := flag.NewFlagSet("run-native", flag.ContinueOnError)
		targets := fs.String("targets-json", "", "Selected native roots as a JSON array")
		if err := fs.Parse(args[1:]); err != nil {
			return err
		}
		if fs.NArg() != 0 {
			return fmt.Errorf("unexpected run-native arguments")
		}
		return runNativeTargets(root, g, *targets, runNativeCommand)
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
		base := fs.String("base", g.DefaultBase, "Base revision")
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
