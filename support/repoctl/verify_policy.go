package main

import (
	"flag"
	"fmt"
	"path/filepath"
	"slices"
	"sort"
	"strings"

	"github.com/BurntSushi/toml"
)

// Policy cases are independent expectations for the graph selected by changed paths.
type policyCase struct {
	Name        *string              `toml:"name"`
	Paths       *[]string            `toml:"paths"`
	Jobs        *[]string            `toml:"jobs"`
	Collections *map[string][]string `toml:"collections"`
	// LocalGroups, when present, is the exact set of check groups that
	// `repoctl check` runs for these paths.
	LocalGroups *[]string `toml:"local_groups"`
}

type policyCases struct {
	// AllLocalGroups, when present, is the exact set of check groups that
	// `repoctl check --all` runs.
	AllLocalGroups *[]string    `toml:"all_local_groups"`
	Cases          []policyCase `toml:"cases"`
}

func readPolicyCases(path string, g graph) (policyCases, error) {
	var file policyCases
	md, err := toml.DecodeFile(path, &file)
	if err != nil {
		return policyCases{}, fmt.Errorf("%s: %w", path, err)
	}
	if keys := md.Undecoded(); len(keys) > 0 {
		return policyCases{}, fmt.Errorf("%s: unknown keys %v", path, keys)
	}
	if len(file.Cases) == 0 {
		return policyCases{}, fmt.Errorf("%s: cases must not be empty", path)
	}
	if err := validateLocalGroupNames(g, path+": all_local_groups", file.AllLocalGroups); err != nil {
		return policyCases{}, err
	}
	knownCollections := map[string]bool{}
	collectionNames := make([]string, 0, len(g.Collections))
	for _, collection := range g.Collections {
		knownCollections[collection.Name] = true
		collectionNames = append(collectionNames, collection.Name)
	}
	sort.Strings(collectionNames)
	seenNames := map[string]bool{}
	for i, c := range file.Cases {
		label := fmt.Sprintf("%s: cases[%d]", path, i)
		if c.Name == nil || strings.TrimSpace(*c.Name) == "" {
			return policyCases{}, fmt.Errorf("%s: name is required", label)
		}
		label = fmt.Sprintf("%s: case %q", path, *c.Name)
		if seenNames[*c.Name] {
			return policyCases{}, fmt.Errorf("%s: duplicate case name", label)
		}
		seenNames[*c.Name] = true
		if c.Paths == nil || len(*c.Paths) == 0 {
			return policyCases{}, fmt.Errorf("%s: paths must not be empty", label)
		}
		if err := unique(*c.Paths, label+" paths"); err != nil {
			return policyCases{}, err
		}
		for _, item := range *c.Paths {
			if !validPath(item) {
				return policyCases{}, fmt.Errorf("%s: unsafe path %q", label, item)
			}
		}
		if c.Jobs == nil {
			return policyCases{}, fmt.Errorf("%s: jobs is required", label)
		}
		if err := unique(*c.Jobs, label+" jobs"); err != nil {
			return policyCases{}, err
		}
		for _, job := range *c.Jobs {
			if !contains(g.Jobs, job) {
				return policyCases{}, fmt.Errorf("%s: unknown job %q", label, job)
			}
		}
		if err := validateLocalGroupNames(g, label+" local_groups", c.LocalGroups); err != nil {
			return policyCases{}, err
		}
		if c.Collections == nil {
			return policyCases{}, fmt.Errorf("%s: collections is required", label)
		}
		for name, values := range *c.Collections {
			if !knownCollections[name] {
				return policyCases{}, fmt.Errorf("%s: unknown collection %q", label, name)
			}
			if err := unique(values, label+" collection "+name); err != nil {
				return policyCases{}, err
			}
		}
		for _, name := range collectionNames {
			if _, ok := (*c.Collections)[name]; !ok {
				return policyCases{}, fmt.Errorf("%s: missing collection %q", label, name)
			}
		}
	}
	return file, nil
}

func validateLocalGroupNames(g graph, label string, names *[]string) error {
	if names == nil {
		return nil
	}
	if err := unique(*names, label); err != nil {
		return err
	}
	for _, name := range *names {
		suite, ok := g.CheckGroups[name]
		if !ok {
			return fmt.Errorf("%s: unknown check group %q", label, name)
		}
		if !suite.RunLocal {
			return fmt.Errorf("%s: check group %q is not local", label, name)
		}
	}
	return nil
}

func compareLocalGroups(label string, got []string, want *[]string) error {
	if want == nil {
		return nil
	}
	sortedWant := slices.Clone(*want)
	sort.Strings(sortedWant)
	if !slices.Equal(got, sortedWant) {
		return fmt.Errorf("%s: local groups = %v, want %v", label, got, sortedWant)
	}
	return nil
}

func sortedSelectedJobs(jobs map[string]bool) []string {
	selected := []string{}
	for job, enabled := range jobs {
		if enabled {
			selected = append(selected, job)
		}
	}
	sort.Strings(selected)
	return selected
}

func verifyPolicyCases(g graph, file policyCases) error {
	if err := compareLocalGroups("--all", selectedLocalGroups(g, allLocalPlan(g)), file.AllLocalGroups); err != nil {
		return err
	}
	for _, c := range file.Cases {
		p, err := g.selectPaths(*c.Paths)
		if err != nil {
			return fmt.Errorf("case %q: %w", *c.Name, err)
		}
		wantJobs := slices.Clone(*c.Jobs)
		sort.Strings(wantJobs)
		if got := sortedSelectedJobs(p.Jobs); !slices.Equal(got, wantJobs) {
			return fmt.Errorf("case %q: jobs = %v, want %v", *c.Name, got, wantJobs)
		}
		if err := compareLocalGroups(fmt.Sprintf("case %q", *c.Name), selectedLocalGroups(g, p), c.LocalGroups); err != nil {
			return err
		}
		collectionNames := make([]string, 0, len(*c.Collections))
		for name := range *c.Collections {
			collectionNames = append(collectionNames, name)
		}
		sort.Strings(collectionNames)
		for _, name := range collectionNames {
			want := (*c.Collections)[name]
			got := p.Collections[name]
			sortedWant := slices.Clone(want)
			sort.Strings(sortedWant)
			if !slices.Equal(got, sortedWant) {
				return fmt.Errorf("case %q: collection %q = %v, want %v", *c.Name, name, got, sortedWant)
			}
		}
	}
	return nil
}

func runVerifyPolicy(root string, g graph, args []string) error {
	fs := flag.NewFlagSet("verify-policy", flag.ContinueOnError)
	casesPath := fs.String("cases", "", "TOML file containing policy contract cases")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if fs.NArg() != 0 || *casesPath == "" {
		return fmt.Errorf("verify-policy requires --cases FILE and no positional arguments")
	}
	path := *casesPath
	if !filepath.IsAbs(path) {
		path = filepath.Join(root, path)
	}
	cases, err := readPolicyCases(path, g)
	if err != nil {
		return err
	}
	if err := verifyPolicyCases(g, cases); err != nil {
		return err
	}
	fmt.Printf("Verified %d policy cases.\n", len(cases.Cases))
	return nil
}
