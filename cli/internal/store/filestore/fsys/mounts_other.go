//go:build !linux

package fsys

import "os"

// checkMount has nothing to read here: only Linux has the mount table CheckMount knows.
func checkMount(*os.File) error { return nil }
