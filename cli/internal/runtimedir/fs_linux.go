//go:build linux

package runtimedir

import "syscall"

type systemFS struct{}

// The file system types that are reached over a network, by their magic numbers.
const (
	magicNFS   = 0x6969
	magicSMB   = 0x517B
	magicCIFS  = 0xFF534D42
	magicSMB2  = 0xFE534D42
	magicCeph  = 0x00C36400
	magicAFS   = 0x5346414F
	magicCoda  = 0x73757245
	magicNCP   = 0x564C
	magicV9FS  = 0x01021997
	magicGPFS  = 0x47504653
	magicLustr = 0x0BD00BD0
)

// IsNetwork implements FS. A FUSE file system is not counted: it is how local encrypted and
// overlay directories are mounted as often as a remote one.
func (systemFS) IsNetwork(path string) (bool, error) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(path, &st); err != nil {
		return false, err
	}
	switch uint32(st.Type) {
	case magicNFS, magicSMB, magicCIFS, magicSMB2, magicCeph, magicAFS, magicCoda, magicNCP, magicV9FS, magicGPFS, magicLustr:
		return true, nil
	}
	return false, nil
}
