package localfs

import "fmt"

// Class is what the state directory's filesystem is known to be. The zero value is Unknown, which
// is refused like Network: a type this build has not been told is local is not assumed local, since
// the check exists to stop a database going quietly corrupt.
type Class int

const (
	// Unknown is a type neither table names.
	Unknown Class = iota
	// Local is a filesystem on a disk of this host, whose locks and shared memory are honest.
	Local
	// Network is shared between hosts, or reached through a user-space driver whose locking cannot
	// be vouched for.
	Network
)

// linuxMagic is a Linux statfs f_type. The kernel's values fit 32 bits, and the field is a signed
// 32-bit integer on 32-bit Linux, so a caller converts through uint32 (see ClassifyLinux) and never
// sign-extends.
type linuxMagic = uint32

type named struct {
	name  string
	class Class
}

// linuxTypes lists the Linux filesystems known to be local or network, by statfs f_type. Everything
// else is Unknown.
var linuxTypes = map[linuxMagic]named{
	// Local, on a disk or in memory of this host.
	0xef53:     {"ext4", Local}, // ext2, ext3 and ext4 share it
	0x58465342: {"xfs", Local},
	0x9123683e: {"btrfs", Local},
	0x01021994: {"tmpfs", Local},
	0x858458f6: {"ramfs", Local},
	0x958458f6: {"hugetlbfs", Local},
	0x794c7630: {"overlayfs", Local},
	0x2fc12fc1: {"zfs", Local},
	0xf2f52010: {"f2fs", Local},
	0x3153464a: {"jfs", Local},
	0x52654973: {"reiserfs", Local},
	0xca451a4e: {"bcachefs", Local},
	0x7366746e: {"ntfs3", Local},
	0x4d44:     {"vfat", Local},
	0x2011bab0: {"exfat", Local},
	0xf15f:     {"ecryptfs", Local},
	0x24051905: {"ubifs", Local},

	// Network or user-space.
	0x6969:     {"nfs", Network},
	0x517b:     {"smb", Network},
	0xfe534d42: {"smb2", Network},
	0xff534d42: {"cifs", Network},
	0xc36400:   {"ceph", Network},
	0x5346414f: {"afs", Network},
	0x73757245: {"coda", Network},
	0x564c:     {"ncp", Network},
	0x1021997:  {"9p", Network},
	0x65735546: {"fuse", Network},
	0x0bd00bd0: {"lustre", Network},
	0x1161970:  {"gfs2", Network},
	0x7461636f: {"ocfs2", Network},
	0xaad7aaea: {"panfs", Network},
	0x19830326: {"beegfs", Network},
	0x786f4256: {"vboxsf", Network},
}

// integer is every type a statfs f_type has on a platform this builds for.
type integer interface {
	~int32 | ~int64 | ~uint32 | ~uint64
}

// ClassifyLinux names a Linux filesystem from its statfs f_type. The value is taken as the 32-bit
// pattern the kernel stored, so a negative int32 on a 32-bit host names the same type as the
// unsigned number in the kernel headers.
func ClassifyLinux[T integer](fType T) Filesystem {
	magic := linuxMagic(fType)
	if n, ok := linuxTypes[magic]; ok {
		return Filesystem{Type: n.name, Class: n.class}
	}
	return Filesystem{Type: fmt.Sprintf("unknown (statfs type 0x%x)", magic)}
}

// darwinTypes lists the macOS f_fstypename values known to be local or network.
var darwinTypes = map[string]Class{
	"apfs": Local, "hfs": Local, "ufs": Local, "msdos": Local, "exfat": Local, "tmpfs": Local, "zfs": Local,
	"nfs": Network, "smbfs": Network, "afpfs": Network, "webdav": Network, "ftp": Network, "macfuse": Network,
	"osxfuse": Network, "fusefs": Network, "cifs": Network,
}

// ClassifyDarwin names a macOS filesystem from its f_fstypename.
func ClassifyDarwin(name string) Filesystem {
	return Filesystem{Type: name, Class: darwinTypes[name]}
}
