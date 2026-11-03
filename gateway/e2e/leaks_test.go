package e2e

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"testing/fstest"
)

type proc struct {
	pid int
	exe string
}

// leakedHosts compares live executable paths relative to the gateway directory.
func leakedHosts(gatewayFS fs.FS, names []string, table []proc) ([]int, error) {
	dirs, err := fs.ReadDir(gatewayFS, ".")
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read gateway directory: %w", err)
	}
	paths := make(map[string]bool)
	for _, dir := range dirs {
		// Hash directories are created as real directories, not symlinks.
		if !dir.IsDir() {
			continue
		}
		for _, name := range names {
			p := path.Join(dir.Name(), name)
			info, err := fs.Stat(gatewayFS, p)
			if err != nil && !errors.Is(err, fs.ErrNotExist) {
				return nil, fmt.Errorf("stat %s: %w", p, err)
			}
			if err == nil && !info.IsDir() {
				paths[p] = true
			}
		}
	}
	var result []int
	for _, p := range table {
		if paths[p.exe] {
			result = append(result, p.pid)
		}
	}
	return result, nil
}

func leakedHostsUnder(gatewayDir string, names []string, table []proc) ([]int, error) {
	canonical, err := filepath.EvalSymlinks(gatewayDir)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("resolve gateway directory: %w", err)
	}
	var relative []proc
	for _, p := range table {
		rel, err := filepath.Rel(canonical, p.exe)
		if err != nil {
			return nil, fmt.Errorf("relative executable %s: %w", p.exe, err)
		}
		if rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
			continue
		}
		relative = append(relative, proc{p.pid, filepath.ToSlash(rel)})
	}
	return leakedHosts(os.DirFS(canonical), names, relative)
}

func TestLeakedHosts(t *testing.T) {
	tests := []struct {
		name  string
		files fstest.MapFS
		table []proc
		want  []int
	}{
		{"both hashes and names", fstest.MapFS{"aaaaaaaaaaaaaaaa/vibesysd": {}, "bbbbbbbbbbbbbbbb/vibesys-host": {}}, []proc{{1, "aaaaaaaaaaaaaaaa/vibesysd"}, {2, "bbbbbbbbbbbbbbbb/vibesys-host"}}, []int{1, 2}},

		{"siblings and directory", fstest.MapFS{"hash/vibesysd-2": {}, "hash/xvibesysd": {}, "hash/vibesysd/child": {}, "hash/vibesys-host": {}}, []proc{{1, "hash/vibesysd-2"}, {2, "hash/xvibesysd"}, {3, "hash/vibesysd"}, {4, "hash/vibesys-host"}}, []int{4}},
		{"absent", fstest.MapFS{}, []proc{{1, "hash/vibesysd"}}, nil},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := leakedHosts(tt.files, []string{"vibesysd", "vibesys-host"}, tt.table)
			if err != nil || !reflect.DeepEqual(got, tt.want) {
				t.Errorf("got %v, %v; want %v", got, err, tt.want)
			}
		})
	}
}

// errorFS injects filesystem failures at the detector's two I/O boundaries.
type errorFS struct {
	fstest.MapFS
	readErr, statErr error
}

func (f errorFS) ReadDir(name string) ([]fs.DirEntry, error) {
	if f.readErr != nil {
		return nil, f.readErr
	}
	return f.MapFS.ReadDir(name)
}
func (f errorFS) Stat(name string) (fs.FileInfo, error) {
	if f.statErr != nil {
		return nil, f.statErr
	}
	return fs.Stat(f.MapFS, name)
}

func TestLeakedHostsErrors(t *testing.T) {
	for _, tt := range []struct {
		name       string
		filesystem errorFS
	}{
		{"unreadable directory", errorFS{readErr: fs.ErrPermission}},
		{"stat failure", errorFS{MapFS: fstest.MapFS{"hash/vibesysd": {}}, statErr: fs.ErrPermission}},
	} {
		t.Run(tt.name, func(t *testing.T) {
			_, err := leakedHosts(tt.filesystem, []string{"vibesysd"}, nil)
			if !errors.Is(err, fs.ErrPermission) {
				t.Fatalf("got error %v", err)
			}
		})
	}
}

func TestLeakedHostsUnderBracketHome(t *testing.T) {
	for _, home := range []string{"ho[me", "ho[m]e"} {
		t.Run(home, func(t *testing.T) {
			gateway := filepath.Join(t.TempDir(), home, ".vibesys", "gateway")
			exe := filepath.Join(gateway, "hash", "vibesys-host")
			if err := os.MkdirAll(filepath.Dir(exe), 0700); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(exe, nil, 0600); err != nil {
				t.Fatal(err)
			}
			got, err := leakedHostsUnder(gateway, []string{"vibesys-host"}, []proc{{9, exe}})
			if err != nil || !reflect.DeepEqual(got, []int{9}) {
				t.Fatalf("got %v, %v", got, err)
			}
		})
	}
}
