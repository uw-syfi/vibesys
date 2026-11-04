package server

import (
	"crypto/sha256"
	"encoding/hex"

	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/reflect/protoreflect"
)

// Field numbers of the keyframe messages this file rewrites, read from the
// generated descriptors so a renumbering cannot silently break the rewrite.
var (
	keyframeEpochs     = fieldNumber((&storev1.Keyframe{}).ProtoReflect(), "epochs")
	versionEpoch       = fieldNumber((&storev1.EpochVersion{}).ProtoReflect(), "epoch")
	versionDescriptors = fieldNumber((&storev1.EpochVersion{}).ProtoReflect(), "descriptors_sha256")
	epochDescriptors   = fieldNumber((&storev1.EpochStarted{}).ProtoReflect(), "descriptors")
)

func fieldNumber(message protoreflect.Message, name protoreflect.Name) protowire.Number {
	return protowire.Number(message.Descriptor().Fields().ByName(name).Number())
}

// descriptorSets are the distinct descriptor sets of a keyframe, by SHA-256 of
// their encoded bytes, in first-use order.
type descriptorSets struct {
	order []string
	data  map[string][]byte
}

// addressDescriptors replaces each epoch's inline descriptors by descriptors_sha256.
// Every other byte, including fields this gateway does not know, is copied as is.
// A keyframe with no inline descriptors is returned unchanged.
func addressDescriptors(keyframe []byte) ([]byte, descriptorSets, error) {
	sets := descriptorSets{data: map[string][]byte{}}
	var out []byte
	changed := false
	rest := keyframe
	for len(rest) > 0 {
		number, kind, n := protowire.ConsumeTag(rest)
		if n < 0 {
			return nil, sets, invalidKeyframe()
		}
		size := protowire.ConsumeFieldValue(number, kind, rest[n:])
		if size < 0 {
			return nil, sets, invalidKeyframe()
		}
		raw := rest[:n+size]
		rest = rest[n+size:]
		if number != keyframeEpochs || kind != protowire.BytesType {
			out = append(out, raw...)
			continue
		}
		version, _ := protowire.ConsumeBytes(raw[n:])
		rewritten, hash, err := addressVersion(version, &sets)
		if err != nil {
			return nil, sets, err
		}
		if hash == "" {
			out = append(out, raw...)
			continue
		}
		changed = true
		out = protowire.AppendTag(out, number, kind)
		out = protowire.AppendBytes(out, rewritten)
	}
	if !changed {
		return keyframe, sets, nil
	}
	return out, sets, nil
}

// addressVersion rewrites one EpochVersion; hash is empty when it had no descriptors.
func addressVersion(version []byte, sets *descriptorSets) ([]byte, string, error) {
	var out, descriptors []byte
	rest := version
	for len(rest) > 0 {
		number, kind, n := protowire.ConsumeTag(rest)
		if n < 0 {
			return nil, "", invalidKeyframe()
		}
		size := protowire.ConsumeFieldValue(number, kind, rest[n:])
		if size < 0 {
			return nil, "", invalidKeyframe()
		}
		raw := rest[:n+size]
		rest = rest[n+size:]
		if number != versionEpoch || kind != protowire.BytesType {
			out = append(out, raw...)
			continue
		}
		epoch, _ := protowire.ConsumeBytes(raw[n:])
		stripped, found, err := stripDescriptors(epoch)
		if err != nil {
			return nil, "", err
		}
		if found != nil {
			descriptors = found
			out = protowire.AppendTag(out, number, kind)
			out = protowire.AppendBytes(out, stripped)
		} else {
			out = append(out, raw...)
		}
	}
	if descriptors == nil {
		return version, "", nil
	}
	digest := sha256.Sum256(descriptors)
	hash := hex.EncodeToString(digest[:])
	if _, seen := sets.data[hash]; !seen {
		sets.order = append(sets.order, hash)
		sets.data[hash] = descriptors
	}
	out = protowire.AppendTag(out, versionDescriptors, protowire.BytesType)
	out = protowire.AppendString(out, hash)
	return out, hash, nil
}

// stripDescriptors removes an EpochStarted's descriptors; found is nil without any.
func stripDescriptors(epoch []byte) (stripped, found []byte, err error) {
	rest := epoch
	for len(rest) > 0 {
		number, kind, n := protowire.ConsumeTag(rest)
		if n < 0 {
			return nil, nil, invalidKeyframe()
		}
		size := protowire.ConsumeFieldValue(number, kind, rest[n:])
		if size < 0 {
			return nil, nil, invalidKeyframe()
		}
		raw := rest[:n+size]
		rest = rest[n+size:]
		if number == epochDescriptors && kind == protowire.BytesType {
			value, _ := protowire.ConsumeBytes(raw[n:])
			if len(value) > 0 {
				found = value
				continue
			}
		}
		stripped = append(stripped, raw...)
	}
	return stripped, found, nil
}

func invalidKeyframe() error {
	return status.Error(codes.DataLoss, "invalid keyframe body")
}
