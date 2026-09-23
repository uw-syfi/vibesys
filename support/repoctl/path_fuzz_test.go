package main

import (
	"strings"
	"testing"
)

func FuzzValidPath(f *testing.F) {
	for _, seed := range []string{
		"",
		"src/module/file.go",
		"/absolute/path",
		"../outside",
		"src/../outside",
		"src//file",
		"src/./file",
		"src/",
		".",
		"..",
	} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, path string) {
		wantValid := path != "" && !strings.HasPrefix(path, "/")
		if wantValid {
			for _, part := range strings.Split(path, "/") {
				if part == "" || part == "." || part == ".." {
					wantValid = false
					break
				}
			}
		}
		if got := validPath(path); got != wantValid {
			t.Fatalf("validPath(%q) = %t, want %t", path, got, wantValid)
		}
	})
}

func FuzzUnderPathBoundary(f *testing.F) {
	for _, seed := range [][2]string{
		{"pkg/file.go", "pkg"},
		{"pkg", "pkg"},
		{"pkg2/file.go", "pkg"},
		{"pkg-extra", "pkg"},
		{"pkg/child/file.go", "pkg"},
		{"", ""},
		{"anything", ""},
	} {
		f.Add(seed[0], seed[1])
	}
	f.Fuzz(func(t *testing.T, path, root string) {
		want := path == root || strings.HasPrefix(path, root+"/")
		if got := under(path, root); got != want {
			t.Fatalf("under(%q, %q) = %t, want %t", path, root, got, want)
		}
		if path != root && under(path, root) && !strings.HasPrefix(path, root+"/") {
			t.Fatalf("under(%q, %q) matched without a path boundary", path, root)
		}
	})
}
