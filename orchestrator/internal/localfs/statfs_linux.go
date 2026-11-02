//go:build linux

package localfs

import "syscall"

// SystemDetector asks the operating system.
type SystemDetector struct{}

// Detect implements Detector with statfs.
func (SystemDetector) Detect(path string) (Filesystem, error) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		return Filesystem{}, err
	}
	return ClassifyLinux(st.Type), nil
}
