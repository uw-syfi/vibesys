//go:build unix

package dockertest

import (
	"os"
	"path/filepath"
)

func userID() int  { return os.Getuid() }
func groupID() int { return os.Getgid() }

// samePath reports whether two paths name the same directory, as a command sees it.
func samePath(a, b string) bool {
	canon := func(p string) string {
		if real, err := filepath.EvalSymlinks(p); err == nil {
			return real
		}
		return p
	}
	return canon(a) == canon(b)
}
