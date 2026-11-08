package assemble

import (
	"fmt"
	"strings"
)

// SubmoduleURL is the URL of the submodule at path, from the output of
// `git config --file .gitmodules --get-regexp '^submodule\..*\.(path|url)$'`: lines of
// "submodule.NAME.path VALUE" and "submodule.NAME.url VALUE". A path that no submodule has, or one
// that has no URL, is an error.
func SubmoduleURL(config, path string) (string, error) {
	paths, urls := map[string]string{}, map[string]string{}
	for _, line := range strings.Split(config, "\n") {
		key, value, ok := strings.Cut(line, " ")
		if !ok || !strings.HasPrefix(key, "submodule.") {
			continue
		}
		key = strings.TrimPrefix(key, "submodule.")
		switch {
		case strings.HasSuffix(key, ".path"):
			paths[strings.TrimSuffix(key, ".path")] = strings.TrimSpace(value)
		case strings.HasSuffix(key, ".url"):
			urls[strings.TrimSuffix(key, ".url")] = strings.TrimSpace(value)
		}
	}
	for name, p := range paths {
		if p == path {
			if urls[name] == "" {
				return "", fmt.Errorf(".gitmodules: submodule %q has no url", name)
			}
			return urls[name], nil
		}
	}
	return "", fmt.Errorf(".gitmodules: no submodule at %s", path)
}

// Gitlink is the commit the repository pins at path, from `git ls-tree HEAD -- path`: one line of
// "MODE TYPE OBJECT\tPATH". Anything but a gitlink there is an error.
func Gitlink(lsTree, path string) (string, error) {
	line := strings.TrimSpace(lsTree)
	meta, name, ok := strings.Cut(line, "\t")
	fields := strings.Fields(meta)
	if !ok || name != path || len(fields) != 3 || fields[0] != "160000" || fields[1] != "commit" {
		return "", fmt.Errorf("%s is not a submodule the repository pins (git ls-tree HEAD says %q)", path, line)
	}
	return fields[2], nil
}
