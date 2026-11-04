package main

import "repoctl/execution"

// forLocalTest returns the graph `repoctl test` plans with. A component that
// declares local_jobs stops selecting every job and collection and selects
// only those jobs, so a change that CI must check everywhere (the repoctl
// policy itself) runs only its own checks on a developer's machine. CI plans
// with the unmodified graph, so its selection does not change.
func (g graph) forLocalTest() graph {
	ignored := append([]string{}, g.IgnoredRoots...)
	if g.Components == nil {
		g.IgnoredRoots = ignored
		return g
	}
	components := make(map[string]component, len(g.Components))
	for id, c := range g.Components {
		if len(c.LocalIgnoreRoots) > 0 {
			kept := make([]string, 0, len(c.Roots))
			for _, root := range c.Roots {
				if contains(c.LocalIgnoreRoots, root) {
					ignored = append(ignored, root)
					continue
				}
				kept = append(kept, root)
			}
			c.Roots = kept
		}
		if len(c.LocalJobs) > 0 {
			c.SelectAllJobs, c.SelectAllCollections = false, false
			c.Jobs = appendUniquePaths(append([]string(nil), c.Jobs...), c.LocalJobs)
		}
		components[id] = c
	}
	g.Components = components
	g.IgnoredRoots = ignored
	return g
}

// underAny reports whether path equals or is under one of the roots.
func underAny(path string, roots []string) bool {
	for _, root := range roots {
		if under(path, root) {
			return true
		}
	}
	return false
}

// anyUnder reports whether any path equals or is under one of the roots.
func anyUnder(paths, roots []string) bool {
	for _, path := range paths {
		if underAny(path, roots) {
			return true
		}
	}
	return false
}

// localSelectionPaths returns the repository paths that narrow a check group in
// `repoctl test`: its local_paths plus the roots and files of its
// local_components. An empty result means the group is not narrowed.
func (g graph) localSelectionPaths(suite execution.Suite) []string {
	paths := append([]string(nil), suite.LocalPaths...)
	for _, id := range suite.LocalComponents {
		c := g.Components[id]
		paths = appendUniquePaths(paths, c.Roots)
		paths = appendUniquePaths(paths, c.Files)
	}
	return paths
}
