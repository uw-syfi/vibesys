package wire_test

import (
	"encoding/json"
	"fmt"
	"math/rand/v2"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// olderRecord is the record as the builds before the messages wrote and read it: a struct with
// these JSON keys. It is the model of an older build sharing the file with this one.
type olderRecord struct {
	User  string    `json:"user"`
	Host  string    `json:"host"`
	PID   int       `json:"pid"`
	Job   string    `json:"job"`
	Since time.Time `json:"since"`
}

type olderReading struct {
	At      time.Time         `json:"at"`
	Outputs map[string]string `json:"outputs"`
}

func anyHolder(r *rand.Rand) core.LeaseHolder {
	word := func() string {
		b := make([]byte, r.IntN(12))
		for i := range b {
			b[i] = "abcXYZ019-_. /\"\\é\n"[r.IntN(18)]
		}
		return strings.ToValidUTF8(string(b), "?")
	}
	return core.LeaseHolder{
		User: word(), Host: word(), PID: r.IntN(1 << 22), Job: core.JobID(word()),
		Since: time.Unix(r.Int64N(4e9), r.Int64N(1e9)).UTC(),
	}
}

func TestALeaseRecordDecodesToTheHolderItEncodedFrom(t *testing.T) {
	r := newRand(5)
	for range 2000 {
		h := anyHolder(r)
		data, err := wire.EncodeLeaseRecord(h)
		if err != nil {
			t.Fatal(err)
		}
		got, err := wire.DecodeLeaseRecord(data)
		if err != nil || !reflect.DeepEqual(got, h) {
			t.Fatalf("%+v encoded as %s decoded as %+v, %v", h, data, got, err)
		}
	}
}

// An older build's record reads here, and what this build writes reads in an older build, even
// with a field a later build adds: the two directions of an upgrade where daemons share the file.
func TestOlderAndNewerBuildsReadEachOthersLeaseRecords(t *testing.T) {
	r := newRand(6)
	for range 2000 {
		h := anyHolder(r)
		older, err := json.Marshal(olderRecord{User: h.User, Host: h.Host, PID: h.PID, Job: string(h.Job), Since: h.Since})
		if err != nil {
			t.Fatal(err)
		}
		got, err := wire.DecodeLeaseRecord(older)
		if err != nil || !reflect.DeepEqual(got, h) {
			t.Fatalf("an older record %s read as %+v, %v; want %+v", older, got, err, h)
		}

		data, err := wire.EncodeLeaseRecord(h)
		if err != nil {
			t.Fatal(err)
		}
		var seen olderRecord
		if err := json.Unmarshal(data, &seen); err != nil || seen.User != h.User || seen.Host != h.Host || seen.PID != h.PID || seen.Job != string(h.Job) || !seen.Since.Equal(h.Since) {
			t.Fatalf("an older build reads %s as %+v, %v; want %+v", data, seen, err, h)
		}

		// A later build adds a field. This build must still read the holder, and so must the older.
		later := strings.TrimSuffix(string(data), "}") + `,"lease_epoch":7,"labels":{"a":"b"}}`
		if strings.HasSuffix(string(data), "{}") {
			t.Fatalf("an empty record %s", data)
		}
		again, err := wire.DecodeLeaseRecord([]byte(later))
		if err != nil || !reflect.DeepEqual(again, h) {
			t.Fatalf("a record with an added field %s read as %+v, %v", later, again, err)
		}
		if json.Unmarshal([]byte(later), &seen) != nil {
			t.Fatalf("an older build cannot read %s", later)
		}
	}
}

func TestAnUnreadableLeaseRecordIsANamedErrorAndNeverAHolder(t *testing.T) {
	good, err := wire.EncodeLeaseRecord(core.LeaseHolder{User: "alice", Host: "host-a", PID: 7, Job: "j1", Since: time.Unix(1e9, 0).UTC()})
	if err != nil {
		t.Fatal(err)
	}
	garbage := [][]byte{
		nil, {}, []byte("   "), []byte("null"), []byte("[]"), []byte("42"), []byte(`"alice"`), []byte("{"),
		[]byte(`{"pid":"seven"}`), []byte(`{"pid":-1}`), []byte(`{"pid":1.5}`), []byte(`{"since":"yesterday"}`),
		[]byte(`{"user":7}`), []byte(`{"user":"a"}{"user":"b"}`), []byte("\x00\x01\x02"), []byte(`{"since":"0000-01-01T00:00:00Z"}`),
	}
	for n := 1; n < len(good); n++ {
		garbage = append(garbage, good[:n]) // every cut of a good record
	}
	r := newRand(7)
	for range 2000 {
		b := make([]byte, r.IntN(40))
		for i := range b {
			b[i] = byte(r.IntN(256))
		}
		garbage = append(garbage, b)
	}
	for _, data := range garbage {
		got, err := wire.DecodeLeaseRecord(data)
		if err == nil {
			// Random bytes can be a record; then it must be one the rules accept and that writes back.
			if _, err := wire.EncodeLeaseRecord(got); err != nil {
				t.Fatalf("%q read as %+v, which cannot be written back: %v", data, got, err)
			}
			if len(strings.TrimSpace(string(data))) > 2 && !strings.HasPrefix(strings.TrimSpace(string(data)), "{") {
				t.Fatalf("%q read as the holder %+v", data, got)
			}
			continue
		}
		if !wire.IsInvalidInput(err) || !strings.Contains(err.Error(), "record") && !strings.Contains(err.Error(), ":") {
			t.Fatalf("%q was refused with %v, which is not a named invalid-input failure", data, err)
		}
		if !reflect.DeepEqual(got, core.LeaseHolder{}) {
			t.Fatalf("%q was refused and still gave the holder %+v", data, got)
		}
	}
}

func TestANodeReadingRoundTripsAndOlderBuildsShareIt(t *testing.T) {
	r := newRand(8)
	for range 1000 {
		at := time.Unix(r.Int64N(4e9), r.Int64N(1e9)).UTC()
		outputs := map[string]string{}
		for i := range 1 + r.IntN(5) {
			outputs[fmt.Sprintf("script-%d", i)] = anyHolder(r).User + "\n"
		}
		data, err := wire.EncodeNodeReading(at, outputs)
		if err != nil {
			t.Fatal(err)
		}
		gotAt, gotOut, err := wire.DecodeNodeReading(data)
		if err != nil || !gotAt.Equal(at) || !reflect.DeepEqual(gotOut, outputs) {
			t.Fatalf("%s decoded as %v %v, %v", data, gotAt, gotOut, err)
		}
		var older olderReading
		if err := json.Unmarshal(data, &older); err != nil || !older.At.Equal(at) || !reflect.DeepEqual(older.Outputs, outputs) {
			t.Fatalf("an older build reads %s as %+v, %v", data, older, err)
		}
		olderData, _ := json.Marshal(olderReading{At: at, Outputs: outputs})
		if gotAt, gotOut, err := wire.DecodeNodeReading(olderData); err != nil || !gotAt.Equal(at) || !reflect.DeepEqual(gotOut, outputs) {
			t.Fatalf("an older reading %s read as %v %v, %v", olderData, gotAt, gotOut, err)
		}
	}
	for _, bad := range []string{"", "{}", `{"at":"2026-01-01T00:00:00Z"}`, `{"outputs":{"a":"b"}}`, `{"at":"x","outputs":{"a":"b"}}`, "[]", `{"at":"2026-01-01T00:00:00Z","outputs":{"a":1}}`} {
		if _, out, err := wire.DecodeNodeReading([]byte(bad)); err == nil || !wire.IsInvalidInput(err) || out != nil {
			t.Fatalf("%q read as a reading: %v, %v", bad, out, err)
		}
	}
}
