package localfs

import "fmt"

// linuxNetwork lists the Linux statfs magic numbers of filesystems that are shared between hosts
// or reached through a user-space driver. A type not listed is taken as local.
var linuxNetwork = map[int64]string{
	0x6969:     "nfs",
	0x517b:     "smb",
	0xfe534d42: "smb2",
	0xff534d42: "cifs",
	0xc36400:   "ceph",
	0x5346414f: "afs",
	0x73757245: "coda",
	0x564c:     "ncp",
	0x1021997:  "9p",
	0x65735546: "fuse",
	0x0bd00bd0: "lustre",
	0x1161970:  "gfs2",
	0x7461636f: "ocfs2",
	0xaad7aaea: "panfs",
	0x19830326: "beegfs",
	0x786f4256: "vboxsf",
}

// ClassifyLinux names a Linux filesystem from its statfs type.
func ClassifyLinux(magic int64) Filesystem {
	if name, ok := linuxNetwork[magic]; ok {
		return Filesystem{Type: name, Network: true}
	}
	return Filesystem{Type: fmt.Sprintf("local (type 0x%x)", magic)}
}

// darwinNetwork lists the macOS filesystem type names that are shared between hosts or user-space.
var darwinNetwork = map[string]bool{
	"nfs": true, "smbfs": true, "afpfs": true, "webdav": true, "ftp": true, "macfuse": true,
	"osxfuse": true, "fusefs": true, "cifs": true,
}

// ClassifyDarwin names a macOS filesystem from its f_fstypename.
func ClassifyDarwin(name string) Filesystem {
	return Filesystem{Type: name, Network: darwinNetwork[name]}
}
