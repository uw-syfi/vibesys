package schemahash_test

import (
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/uw-syfi/vibesys/gateway/schemahash"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/descriptorpb"
)

func hash(t *testing.T, files ...*descriptorpb.FileDescriptorProto) string {
	t.Helper()
	image, err := proto.Marshal(&descriptorpb.FileDescriptorSet{File: files})
	if err != nil {
		t.Fatal(err)
	}
	result, err := schemahash.Hash(image, "example.v1")
	if err != nil {
		t.Fatal(err)
	}
	return result
}

func FuzzSchemaHash(f *testing.F) {
	f.Add("a.proto", "b.proto", "documentation")
	f.Fuzz(func(t *testing.T, first, second, comment string) {
		if !utf8.ValidString(first) || !utf8.ValidString(second) || !utf8.ValidString(comment) {
			t.Skip("protobuf strings require UTF-8")
		}
		// Distinct filenames keep file ordering unambiguous for arbitrary seeds.
		a := &descriptorpb.FileDescriptorProto{Name: proto.String("a/" + first), Package: proto.String("example.v1")}
		b := &descriptorpb.FileDescriptorProto{Name: proto.String("b/" + second), Package: proto.String("example.v1")}
		expected := hash(t, a, b)
		a.SourceCodeInfo = &descriptorpb.SourceCodeInfo{Location: []*descriptorpb.SourceCodeInfo_Location{{LeadingComments: proto.String(comment)}}}
		other := &descriptorpb.FileDescriptorProto{Name: proto.String("other.proto"), Package: proto.String("other.v1")}
		if got := hash(t, b, other, a); got != expected {
			t.Fatalf("order/comments/import changed hash: %s != %s", got, expected)
		}
		b.Options = &descriptorpb.FileOptions{Deprecated: proto.Bool(true)}
		if hash(t, a, b) == expected {
			t.Fatal("option change must change identity")
		}
	})
}

func TestHashRejectsMissingPackageAndMalformedImage(t *testing.T) {
	if _, err := schemahash.Hash(nil, "missing.v1"); err == nil || !strings.Contains(err.Error(), "missing.v1") {
		t.Fatalf("missing package error: %v", err)
	}
	if _, err := schemahash.Hash([]byte{0xff}, "example.v1"); err == nil {
		t.Fatal("accepted malformed image")
	}
}
