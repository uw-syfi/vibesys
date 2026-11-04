//go:build darwin

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
	name := make([]byte, 0, len(st.Fstypename))
	for _, c := range st.Fstypename {
		if c == 0 {
			break
		}
		name = append(name, byte(c))
	}
	return ClassifyDarwin(string(name)), nil
}
