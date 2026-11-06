package privatesocket_test

import (
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"testing/quick"

	"github.com/uw-syfi/vibesys/orchestrator/privatesocket"
)

// For any natural path, the socket's place fits in a socket address, is the natural path itself
// whenever that fits, is the same on every call, and is never shared by two natural paths.
func TestAPlaceAlwaysFitsAndOnlyMovesWhatDoesNot(t *testing.T) {
	seen := map[string]string{}
	if err := quick.Check(func(name []byte, extra uint8) bool {
		natural := filepath.Clean("/" + strings.Repeat("d", int(extra)) + "/" + string(name))
		place := privatesocket.Place(natural)
		switch {
		case place != privatesocket.Place(natural):
			t.Logf("%q: two answers", natural)
			return false
		case !privatesocket.Fits(place):
			t.Logf("%q: %q does not fit", natural, place)
			return false
		case privatesocket.Fits(natural) != (place == natural):
			t.Logf("%q: moved to %q although it fits, or kept although it does not", natural, place)
			return false
		}
		if other, ok := seen[place]; ok && other != natural {
			t.Logf("%q and %q share %q", other, natural, place)
			return false
		}
		seen[place] = natural
		return true
	}, nil); err != nil {
		t.Fatal(err)
	}
}

// A socket whose natural path is too long answers at its place, in a directory only this user can
// reach, and the same place is found again from the natural path alone.
func TestASocketTooLongForItsPlaceAnswersAtTheShortPlace(t *testing.T) {
	natural := filepath.Join(t.TempDir(), strings.Repeat("s", 120), "sock")
	place := privatesocket.Place(natural)
	if place == natural {
		t.Fatalf("%s was kept", natural)
	}
	if err := privatesocket.EnsureDirectory(place); err != nil {
		t.Fatal(err)
	}
	listener, err := privatesocket.Listen(place)
	if err != nil {
		t.Fatal(err)
	}
	defer privatesocket.Remove(place)
	defer listener.Close()
	info, err := os.Stat(filepath.Dir(place))
	if err != nil || info.Mode().Perm()&0o077 != 0 {
		t.Fatalf("the directory of %s is %v, %v; want one closed to others", place, info, err)
	}
	go func() {
		if c, err := listener.Accept(); err == nil {
			c.Close()
		}
	}()
	c, err := net.Dial("unix", privatesocket.Place(natural))
	if err != nil {
		t.Fatal(err)
	}
	c.Close()
}
