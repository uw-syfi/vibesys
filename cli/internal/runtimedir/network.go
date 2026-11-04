package runtimedir

import "slices"

// networkMagics are the file system types that are reached over a network, by the magic numbers
// Linux's statfs gives them: NFS, SMB, CIFS, SMB2, Ceph, AFS, Coda, NCP, 9P, GPFS and Lustre. A
// FUSE file system is not counted: it is how local encrypted and overlay directories are mounted as
// often as a remote one.
var networkMagics = []uint32{
	0x6969, 0x517B, 0xFF534D42, 0xFE534D42, 0x00C36400, 0x5346414F, 0x73757245, 0x564C, 0x01021997,
	0x47504653, 0x0BD00BD0,
}

// NetworkMagics are the statfs types of the network file systems of Linux, for a check made on
// another Linux machine, such as with stat -f, to judge as IsNetwork judges here.
func NetworkMagics() []uint32 { return slices.Clone(networkMagics) }
