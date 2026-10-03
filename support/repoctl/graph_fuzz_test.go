package main

import (
	"reflect"
	"strings"
	"testing"
)

// FuzzGraphSelection checks generated repository layouts against a small
// reference model based on path segments and graph reachability.
func FuzzGraphSelection(f *testing.F) {
	for _, seed := range [][]byte{
		{0},
		{1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0},
		{2, 255, 15, 3, 7, 31, 2, 11, 5, 23, 17, 13, 9, 19, 29, 37},
		{3, 1, 2, 4, 8, 16, 32, 64, 128, 255, 0, 1, 2, 3, 4, 5},
		{4, 42, 81, 123, 92, 17, 239, 11, 67, 88, 19, 201, 5, 9, 37, 43},
		func() []byte {
			data := make([]byte, 96)
			for i := range data {
				data[i] = byte(i*37 + 13)
			}
			return data
		}(),
	} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, data []byte) {
		if len(data) > 128 {
			t.Skip()
		}
		byteAt := func(i int) byte {
			if i < len(data) {
				return data[i]
			}
			return 0
		}

		roots := []string{"src", "src/core", "src/core/deep", "src/corex", "web", "web/app", "src/core"}
		groups := []string{"", "main", "other"}
		g := graph{
			Components:   map[string]component{},
			Jobs:         []string{"build", "lint", "test"},
			IgnoredRoots: []string{"vendor"},
			IgnoredFiles: []string{"LICENSE"},
		}
		for i, root := range roots {
			id := string(rune('a' + i))
			c := component{
				ID: id, Roots: []string{root},
				OwnershipGroup: groups[byteAt(2*i+1)%byte(len(groups))],
				Jobs:           []string{g.Jobs[byteAt(2*i+2)%byte(len(g.Jobs))]},
			}
			for j := 0; j < i; j++ {
				if byteAt(20+i*8+j)&1 != 0 {
					c.DependsOn = append(c.DependsOn, string(rune('a'+j)))
				}
			}
			if byteAt(80+i)&1 != 0 {
				c.SelectAllJobs = true
			}
			if err := g.add(c); err != nil {
				t.Fatal(err)
			}
		}
		if err := g.add(component{ID: "file", Files: []string{"README.md"}, Jobs: []string{"lint"}, OwnershipGroup: "main"}); err != nil {
			t.Fatal(err)
		}

		candidates := []string{
			"src/file.go", "src/core/file.go", "src/core/deep/file.go",
			"src/corex/file.go", "web/file.ts", "web/app/file.ts",
			"README.md", "vendor/external.go", "LICENSE", "mystery/file.go",
		}
		paths := []string{candidates[int(byteAt(0))%len(candidates)]}
		if byteAt(1)&1 != 0 {
			paths = append(paths, candidates[int(byteAt(3))%len(candidates)])
		}
		for _, path := range paths {
			got := g.owners(path)
			want := modelOwners(g, path)
			if !reflect.DeepEqual(got, want) {
				t.Fatalf("owners(%q) = %v, want %v; graph = %#v", path, got, want, g.Components)
			}
		}

		got, err := g.selectPaths(paths)
		selected := map[string]bool{}
		unknown := []string{}
		for _, path := range paths {
			owners := modelOwners(g, path)
			if len(owners) == 0 && path != "LICENSE" && !strings.HasPrefix(path, "vendor/") {
				unknown = append(unknown, path)
			}
			for _, id := range owners {
				selected[id] = true
			}
		}
		if len(unknown) > 0 {
			if err == nil {
				t.Fatalf("unowned paths %q were accepted", unknown)
			}
			for _, path := range unknown {
				if !strings.Contains(err.Error(), path) {
					t.Fatalf("error %q omits unowned path %q", err, path)
				}
			}
			return
		}
		if err != nil {
			t.Fatal(err)
		}
		// Compute reachability by repeated edge relaxation, independently of
		// the implementation's reverse adjacency list and queue traversal.
		for changed := true; changed; {
			changed = false
			for _, id := range g.Order {
				if selected[id] {
					continue
				}
				for _, dependency := range g.Components[id].DependsOn {
					if selected[dependency] {
						selected[id] = true
						changed = true
						break
					}
				}
			}
		}
		if len(got.Components) != len(selected) {
			t.Fatalf("selected components = %v, want %v", got.Components, selected)
		}
		wantJobs := map[string]bool{"build": false, "lint": false, "test": false}
		for id := range selected {
			if _, ok := got.Components[id]; !ok {
				t.Fatalf("missing selected component %q from %v", id, got.Components)
			}
			c := g.Components[id]
			for _, job := range c.Jobs {
				wantJobs[job] = true
			}
			if c.SelectAllJobs {
				for job := range wantJobs {
					wantJobs[job] = true
				}
			}
		}
		if !reflect.DeepEqual(got.Jobs, wantJobs) {
			t.Fatalf("jobs = %v, want %v; selected = %v", got.Jobs, wantJobs, selected)
		}
		for job, enabled := range wantJobs {
			if enabled && len(got.JobReasons[job]) == 0 {
				t.Fatalf("selected job %q has no reason", job)
			}
		}
	})
}

func modelOwners(g graph, path string) []string {
	matching := map[string]int{}
	deepest := map[string]int{}
	for _, id := range g.Order {
		c := g.Components[id]
		depth := -1
		for _, root := range c.Roots {
			if path == root || strings.HasPrefix(path, root+"/") {
				if n := strings.Count(root, "/") + 1; n > depth {
					depth = n
				}
			}
		}
		exactFile := false
		for _, file := range c.Files {
			if file == path {
				exactFile = true
			}
		}
		if depth >= 0 || exactFile {
			matching[id] = depth
			if c.OwnershipGroup != "" && depth > deepest[c.OwnershipGroup] {
				deepest[c.OwnershipGroup] = depth
			}
		}
	}
	owners := []string{}
	for _, id := range g.Order {
		depth, ok := matching[id]
		if !ok {
			continue
		}
		group := g.Components[id].OwnershipGroup
		if group == "" || depth < 0 || depth == deepest[group] {
			owners = append(owners, id)
		}
	}
	if len(owners) == 0 {
		return []string{}
	}
	return owners
}
