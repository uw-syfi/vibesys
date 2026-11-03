package e2e

import (
	"io/fs"
	"path"
	"reflect"
	"testing"
	"testing/fstest"
)

type proc struct {
	pid int
	exe string
}

// leakedHosts compares live executable paths relative to the gateway directory.
func leakedHosts(gatewayFS fs.FS, names []string, table []proc) []int {
	dirs, _ := fs.ReadDir(gatewayFS, ".")
	paths := make(map[string]bool)
	for _, dir := range dirs {
		if !dir.IsDir() {
			continue
		}
		for _, name := range names {
			p := path.Join(dir.Name(), name)
			info, err := fs.Stat(gatewayFS, p)
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
	return result
}

func TestLeakedHosts(t *testing.T) {
	tests := []struct {
		name  string
		files fstest.MapFS
		table []proc
		want  []int
	}{
		{"both hashes and names", fstest.MapFS{"aaaaaaaaaaaaaaaa/vibesysd": {}, "bbbbbbbbbbbbbbbb/vibesys-host": {}}, []proc{{1, "aaaaaaaaaaaaaaaa/vibesysd"}, {2, "bbbbbbbbbbbbbbbb/vibesys-host"}}, []int{1, 2}},
		{"bracket in home", fstest.MapFS{"home[/gateway/hash/vibesysd": {}}, []proc{{3, "hash/vibesysd"}}, []int{3}},
		{"siblings and directory", fstest.MapFS{"hash/vibesysd-2": {}, "hash/xvibesysd": {}, "hash/vibesysd/child": {}}, []proc{{1, "hash/vibesysd-2"}, {2, "hash/xvibesysd"}, {3, "hash/vibesysd"}}, nil},
		{"absent", fstest.MapFS{}, []proc{{1, "hash/vibesysd"}}, nil},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var gatewayFS fs.FS = tt.files
			if tt.name == "bracket in home" {
				var err error
				gatewayFS, err = fs.Sub(tt.files, "home[/gateway")
				if err != nil {
					t.Fatal(err)
				}
			}
			if got := leakedHosts(gatewayFS, []string{"vibesysd", "vibesys-host"}, tt.table); !reflect.DeepEqual(got, tt.want) {
				t.Errorf("got %v, want %v", got, tt.want)
			}
		})
	}
}
