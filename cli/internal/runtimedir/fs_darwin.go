//go:build darwin

package runtimedir

import "syscall"

type systemFS struct{}

// IsNetwork implements FS.
func (systemFS) IsNetwork(path string) (bool, error) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		return false, err
	}
	var name []byte
	for _, c := range st.Fstypename {
		if c == 0 {
			break
		}
		name = append(name, byte(c))
	}
	switch string(name) {
	case "nfs", "smbfs", "afpfs", "webdav", "autofs":
		return true, nil
	}
	return false, nil
}
