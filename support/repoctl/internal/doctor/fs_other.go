//go:build !linux

package doctor

func filesystemOf(path string) (Filesystem, error) {
	if _, err := nearestExisting(path); err != nil {
		return Filesystem{}, err
	}
	// The type and device are unknown here; the checks that need them pass.
	return Filesystem{}, nil
}
