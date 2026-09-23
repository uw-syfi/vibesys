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
}

type policyCases struct {
	Cases []policyCase `toml:"cases"`
}

func readPolicyCases(path string, g graph) ([]policyCase, error) {
	var file policyCases
	md, err := toml.DecodeFile(path, &file)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	if keys := md.Undecoded(); len(keys) > 0 {
		return nil, fmt.Errorf("%s: unknown keys %v", path, keys)
	}
	if len(file.Cases) == 0 {
		return nil, fmt.Errorf("%s: cases must not be empty", path)
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
			return nil, fmt.Errorf("%s: name is required", label)
		}
		label = fmt.Sprintf("%s: case %q", path, *c.Name)
		if seenNames[*c.Name] {
			return nil, fmt.Errorf("%s: duplicate case name", label)
		}
		seenNames[*c.Name] = true
		if c.Paths == nil || len(*c.Paths) == 0 {
			return nil, fmt.Errorf("%s: paths must not be empty", label)
		}
		if err := unique(*c.Paths, label+" paths"); err != nil {
			return nil, err
		}
		for _, item := range *c.Paths {
			if !validPath(item) {
				return nil, fmt.Errorf("%s: unsafe path %q", label, item)
			}
		}
		if c.Jobs == nil {
			return nil, fmt.Errorf("%s: jobs is required", label)
		}
		if err := unique(*c.Jobs, label+" jobs"); err != nil {
			return nil, err
		}
		for _, job := range *c.Jobs {
			if !contains(g.Jobs, job) {
				return nil, fmt.Errorf("%s: unknown job %q", label, job)
			}
		}
		if c.Collections == nil {
			return nil, fmt.Errorf("%s: collections is required", label)
		}
		for name, values := range *c.Collections {
			if !knownCollections[name] {
				return nil, fmt.Errorf("%s: unknown collection %q", label, name)
			}
			if err := unique(values, label+" collection "+name); err != nil {
				return nil, err
			}
		}
		for _, name := range collectionNames {
			if _, ok := (*c.Collections)[name]; !ok {
				return nil, fmt.Errorf("%s: missing collection %q", label, name)
			}
		}
	}
	return file.Cases, nil
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

func verifyPolicyCases(g graph, cases []policyCase) error {
	for _, c := range cases {
		p, err := g.selectPaths(*c.Paths)
		if err != nil {
			return fmt.Errorf("case %q: %w", *c.Name, err)
		}
		wantJobs := slices.Clone(*c.Jobs)
		sort.Strings(wantJobs)
		if got := sortedSelectedJobs(p.Jobs); !slices.Equal(got, wantJobs) {
			return fmt.Errorf("case %q: jobs = %v, want %v", *c.Name, got, wantJobs)
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
	fmt.Printf("Verified %d policy cases.\n", len(cases))
	return nil
}
