package clientcli

import (
	"io/fs"
	"path"
	"strings"
)

// gitRoot walks up from dir looking for a .git, a directory or a file as a worktree has, and
// returns the directory that holds it, or "" when there is none.
func gitRoot(files fs.FS, dir string) string {
	for dir = path.Clean(dir); ; dir = path.Dir(dir) {
		rel := strings.TrimPrefix(path.Join(dir, ".git"), "/")
		if _, err := fs.Stat(files, rel); err == nil {
			return dir
		}
		if dir == "/" || dir == "." {
			return ""
		}
	}
}

// absolute makes p absolute against dir, without touching the file system.
func absolute(dir, p string) string {
	if path.IsAbs(p) {
		return path.Clean(p)
	}
	return path.Join(dir, p)
}

// repository is the repository a verb works in: the flag made absolute, else the git root of the
// working directory.
func (e Env) repository(flag string) (string, error) {
	if flag != "" {
		return absolute(e.Dir, flag), nil
	}
	if root := gitRoot(e.FS, e.Dir); root != "" {
		return root, nil
	}
	return "", usageError("%s is not in a git repository: run from inside one, or pass --repository DIR", e.Dir)
}
