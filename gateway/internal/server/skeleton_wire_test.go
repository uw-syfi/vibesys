package server_test

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"strings"
	"testing"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/descriptorpb"
)

// descriptorSet is a set of the given encoded size, distinct per name.
func descriptorSet(name string, size int) *descriptorpb.FileDescriptorSet {
	return &descriptorpb.FileDescriptorSet{File: []*descriptorpb.FileDescriptorProto{{
		Name: proto.String(name + ".proto"), Package: proto.String(strings.Repeat("p", size)),
	}}}
}

func epochWith(seq uint64, set *descriptorpb.FileDescriptorSet) *storev1.EpochVersion {
	return &storev1.EpochVersion{Seq: seq, Epoch: &storev1.EpochStarted{RunId: "run-1", Epoch: uint32(seq), Descriptors: set}}
}

// storeRun writes count one-record frames, a head committing them, and one
// keyframe through each given sequence holding that sequence's epochs.
func storeRun(t *testing.T, f fixture, count int, keyframes map[uint64][]*storev1.EpochVersion) {
	t.Helper()
	_, offsets := writeFrames(t, f, count)
	publishHead(t, f, head(proto.Uint64(uint64(count))))
	if err := os.MkdirAll(filepath.Join(f.run, "keyframes"), 0700); err != nil {
		t.Fatal(err)
	}
	for seq, epochs := range keyframes {
		data := encode(t, &storev1.Keyframe{ThroughSeq: seq, LogOffset: uint64(offsets[seq]), Epochs: epochs})
		write(t, filepath.Join(f.run, "keyframes", fmt.Sprintf("%d.pb", seq)), false, data)
	}
}

// restored is what a reader makes of a Skeleton reply: the keyframe with each
// epoch's descriptors verified against their hash and put back.
func restored(t *testing.T, reply *apiv1.SkeletonResponse, known map[string]*descriptorpb.FileDescriptorSet) *storev1.Keyframe {
	t.Helper()
	sets := map[string]*descriptorpb.FileDescriptorSet{}
	for hash, set := range known {
		sets[hash] = set
	}
	for _, sent := range reply.DescriptorSets {
		digest := sha256.Sum256(sent.Data)
		if hex.EncodeToString(digest[:]) != sent.Sha256 {
			t.Fatalf("descriptor set %s does not hash to its name", sent.Sha256)
		}
		set := &descriptorpb.FileDescriptorSet{}
		if err := proto.Unmarshal(sent.Data, set); err != nil {
			t.Fatal(err)
		}
		sets[sent.Sha256] = set
	}
	var keyframe storev1.Keyframe
	if err := proto.Unmarshal(reply.Keyframe, &keyframe); err != nil {
		t.Fatal(err)
	}
	for _, version := range keyframe.Epochs {
		if version.DescriptorsSha256 == "" {
			continue
		}
		set, ok := sets[version.DescriptorsSha256]
		if !ok || version.Epoch.Descriptors != nil {
			t.Fatalf("epoch %d names %q without a set, or also carries one", version.Seq, version.DescriptorsSha256)
		}
		version.Epoch.Descriptors = set
		version.DescriptorsSha256 = ""
	}
	return &keyframe
}

func TestSkeletonNamesDescriptorsByHashAndSendsEachSetOnce(t *testing.T) {
	f := start(t)
	shared, other := descriptorSet("shared", 90<<10), descriptorSet("other", 50<<10)
	stored := []*storev1.EpochVersion{epochWith(1, shared), epochWith(2, shared), epochWith(3, other), epochWith(4, nil)}
	storeRun(t, f, 6, map[uint64][]*storev1.EpochVersion{6: stored})
	c := client(t, f, false)
	got, err := c.Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: "run-1"})
	if err != nil {
		t.Fatal(err)
	}
	if len(got.DescriptorSets) != 2 || !got.Complete || len(got.Records) != 0 {
		t.Fatalf("sets=%d complete=%v records=%d", len(got.DescriptorSets), got.Complete, len(got.Records))
	}
	storedSize := proto.Size(&storev1.Keyframe{Epochs: stored})
	if len(got.Keyframe) > storedSize/10 {
		t.Fatalf("keyframe kept its descriptors: %d of %d bytes", len(got.Keyframe), storedSize)
	}
	want := &storev1.Keyframe{ThroughSeq: 6, Epochs: stored}
	have := restored(t, got, nil)
	have.LogOffset = 0
	if !proto.Equal(have, want) {
		t.Fatal("restored keyframe differs from the stored one")
	}
	hashes := []string{}
	for _, sent := range got.DescriptorSets {
		hashes = append(hashes, sent.Sha256)
	}
	again, err := c.Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: "run-1", KnownDescriptorsSha256: hashes})
	if err != nil {
		t.Fatal(err)
	}
	if len(again.DescriptorSets) != 0 || len(again.Keyframe) != len(got.Keyframe) {
		t.Fatalf("a client holding every set was sent %d", len(again.DescriptorSets))
	}
}

// Stored keyframes at the wire cap were dropped for descriptors the client never
// needs to receive again; their wire form fits.
func TestSkeletonKeyframeFitsOnceDescriptorsAreAddressed(t *testing.T) {
	f := start(t)
	big := descriptorSet("big", 400<<10)
	stored := []*storev1.EpochVersion{epochWith(1, big), epochWith(2, big), epochWith(3, big)}
	if proto.Size(&storev1.Keyframe{Epochs: stored}) <= 1<<20 {
		t.Fatal("fixture must exceed the wire cap when stored")
	}
	storeRun(t, f, 6, map[uint64][]*storev1.EpochVersion{6: stored})
	got, err := client(t, f, false).Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: "run-1"})
	if err != nil {
		t.Fatal(err)
	}
	if proto.Size(got) > 1<<20 || len(got.Keyframe) == 0 || !got.Complete || len(got.Records) != 0 {
		t.Fatalf("size=%d keyframe=%d complete=%v records=%d", proto.Size(got), len(got.Keyframe), got.Complete, len(got.Records))
	}
}

// A newest keyframe that cannot fit must not discard every keyframe: an older,
// smaller one plus the records after it is still far less than a full replay.
func TestSkeletonFallsBackToAnOlderKeyframe(t *testing.T) {
	f := start(t)
	var many []*storev1.EpochVersion
	for i := 1; i <= 12; i++ {
		many = append(many, epochWith(uint64(i), descriptorSet(fmt.Sprint("set", i), 100<<10)))
	}
	older := []*storev1.EpochVersion{epochWith(1, descriptorSet("set1", 100<<10))}
	storeRun(t, f, 10, map[uint64][]*storev1.EpochVersion{4: older, 10: many})
	got, err := client(t, f, false).Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: "run-1"})
	if err != nil {
		t.Fatal(err)
	}
	keyframe := restored(t, got, nil)
	if keyframe.ThroughSeq != 4 || len(got.Records) != 6 || !got.Complete || got.NextSeq != 11 {
		t.Fatalf("through=%d records=%d complete=%v next=%d", keyframe.ThroughSeq, len(got.Records), got.Complete, got.NextSeq)
	}
}

// Over generated keyframes the wire form restores to the stored keyframe, sends
// each distinct unlisted set exactly once and keeps the reply under the cap.
func TestSkeletonDescriptorsRoundTripOverGeneratedKeyframes(t *testing.T) {
	f := start(t)
	random := rand.New(rand.NewSource(7))
	pool := []*descriptorpb.FileDescriptorSet{descriptorSet("a", 3000), descriptorSet("b", 60000), descriptorSet("c", 1)}
	c := client(t, f, false)
	for round := 0; round < 40; round++ {
		var stored []*storev1.EpochVersion
		distinct := map[*descriptorpb.FileDescriptorSet]bool{}
		for i := 0; i < random.Intn(6); i++ {
			var set *descriptorpb.FileDescriptorSet
			if random.Intn(5) > 0 {
				set = pool[random.Intn(len(pool))]
				distinct[set] = true
			}
			stored = append(stored, epochWith(uint64(i+1), set))
		}
		storeRun(t, f, 5, map[uint64][]*storev1.EpochVersion{5: stored})
		got, err := c.Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: "run-1"})
		if err != nil {
			t.Fatal(err)
		}
		if len(got.DescriptorSets) != len(distinct) || proto.Size(got) > 1<<20 {
			t.Fatalf("round %d: %d sets for %d distinct, %d bytes", round, len(got.DescriptorSets), len(distinct), proto.Size(got))
		}
		have := restored(t, got, nil)
		have.LogOffset = 0
		if !proto.Equal(have, &storev1.Keyframe{ThroughSeq: 5, Epochs: stored}) {
			t.Fatalf("round %d: restored keyframe differs", round)
		}
	}
}
