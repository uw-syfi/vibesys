// Package schemahash derives schema identity from the embedded descriptors.
package schemahash

import (
	"crypto/sha256"
	"fmt"
	"sort"

	_ "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	_ "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protodesc"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/reflect/protoregistry"
	"google.golang.org/protobuf/types/descriptorpb"
)

// APISchemaHash and StoreSchemaHash identify source descriptors, including options.
var (
	APISchemaHash   = packageHash("vibesys.api.v1")
	StoreSchemaHash = packageHash("vibesys.store.v1")
	HostSchemaHash  = packageHash("vibesys.host.v1")
)

func packageHash(name protoreflect.FullName) string {
	set := &descriptorpb.FileDescriptorSet{}
	protoregistry.GlobalFiles.RangeFilesByPackage(name, func(file protoreflect.FileDescriptor) bool {
		descriptor := protodesc.ToFileDescriptorProto(file)
		descriptor.SourceCodeInfo = nil
		set.File = append(set.File, descriptor)
		return true
	})
	encoded, err := proto.Marshal(set)
	if err != nil {
		panic(err)
	}
	hash, err := Hash(encoded, string(name))
	if err != nil {
		panic(err)
	}
	return hash
}

// Hash returns the canonical identity of a nonempty package in a descriptor set.
// Source comments/locations and imported packages are excluded; options remain.
func Hash(image []byte, name string) (string, error) {
	set := &descriptorpb.FileDescriptorSet{}
	if err := proto.Unmarshal(image, set); err != nil {
		return "", err
	}
	selected := set.File[:0]
	for _, file := range set.File {
		if file.GetPackage() == name {
			file.SourceCodeInfo = nil
			selected = append(selected, file)
		}
	}
	set.File = selected
	if len(set.File) == 0 {
		return "", fmt.Errorf("no descriptors in package %s", name)
	}
	sort.Slice(set.File, func(i, j int) bool { return set.File[i].GetName() < set.File[j].GetName() })
	encoded, err := (proto.MarshalOptions{Deterministic: true}).Marshal(set)
	if err != nil {
		return "", err
	}
	return fmt.Sprintf("%x", sha256.Sum256(canonicalDescriptor(set.ProtoReflect().Descriptor(), encoded))), nil
}
