//go:build linux

package doctor

import (
	"os"
	"syscall"
)

func filesystemOf(path string) (Filesystem, error) {
	real, err := nearestExisting(path)
	if err != nil {
		return Filesystem{}, err
	}
	var fs Filesystem
	if info, err := os.Stat(real); err == nil {
		if st, ok := info.Sys().(*syscall.Stat_t); ok {
			fs.Device = uint64(st.Dev)
		}
	}
	if data, err := os.ReadFile("/proc/self/mountinfo"); err == nil {
		fs.Type = FilesystemFromMountInfo(string(data), real)
	}
	return fs, nil
}
