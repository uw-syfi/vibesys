package fsys

import (
	"fmt"
	"os"
	"syscall"
)

// checkMount refuses a file on a mount whose locks stop at this machine. A table that cannot be
// read refuses too: the check would otherwise pass by saying nothing.
func checkMount(f *os.File) error {
	var st syscall.Stat_t
	if err := syscall.Fstat(int(f.Fd()), &st); err != nil {
		return err
	}
	table, err := os.ReadFile("/proc/self/mountinfo")
	if err != nil {
		return fmt.Errorf("%w: the mount table cannot be read: %w", ErrNoLocks, err)
	}
	// The split of a device number, from the kernel's kdev_t.h.
	major := uint32((st.Dev>>8)&0xfff | (st.Dev>>32)&0xfffff000)
	minor := uint32(st.Dev&0xff | (st.Dev>>12)&0xffffff00)
	return CheckMount(table, major, minor)
}
