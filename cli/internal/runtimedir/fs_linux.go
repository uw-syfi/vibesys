//go:build linux

package runtimedir

import (
	"slices"
	"syscall"
)

type systemFS struct{}

// IsNetwork implements FS. A FUSE file system is not counted: it is how local encrypted and
// overlay directories are mounted as often as a remote one.
func (systemFS) IsNetwork(path string) (bool, error) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		return false, err
	}
	return slices.Contains(networkMagics, uint32(st.Type)), nil
}
