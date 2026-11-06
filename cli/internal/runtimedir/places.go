package runtimedir

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/encoding/protojson"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// placesFile is where the directory records where its daemon keeps state.
func (d Dir) placesFile() string { return filepath.Join(d.path, "daemon-places.json") }

// Places is what the directory records, or nil when it records nothing yet. A file that cannot be
// read as the message is an error naming the file, and never read as nothing: a daemon that guessed
// would open a second store.
func (d Dir) Places() (*cliv1.DaemonPlaces, error) {
	data, err := os.ReadFile(d.placesFile())
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("reading the daemon's places: %w", err)
	}
	places := &cliv1.DaemonPlaces{}
	if err := protojson.Unmarshal(data, places); err != nil {
		return nil, unreadablePlaces(d.placesFile(), err)
	}
	if err := protovalidate.Validate(places); err != nil {
		return nil, unreadablePlaces(d.placesFile(), err)
	}
	return places, nil
}

func unreadablePlaces(file string, err error) error {
	return invalid("%s does not say where the daemon keeps its state: %v; with no daemon running, remove the file and start one from the environment that names the place", file, err)
}

// RecordPlaces records stateHome unless the directory records places already, and returns what the
// directory records. Two callers at once record one answer, whichever links its file first, and a
// reader never sees a part of one.
func (d Dir) RecordPlaces(stateHome string) (*cliv1.DaemonPlaces, error) {
	if recorded, err := d.Places(); err != nil || recorded != nil {
		return recorded, err
	}
	places := &cliv1.DaemonPlaces{StateHome: stateHome}
	if err := protovalidate.Validate(places); err != nil {
		return nil, invalid("the daemon's state home %q must be an absolute path: this environment names no home directory", stateHome)
	}
	data, err := protojson.Marshal(places)
	if err != nil {
		return nil, err
	}
	tmp, err := os.CreateTemp(d.path, "daemon-places-*.tmp")
	if err != nil {
		return nil, fmt.Errorf("recording the daemon's places: %w", err)
	}
	defer os.Remove(tmp.Name())
	_, err = tmp.Write(data)
	if closed := tmp.Close(); err == nil {
		err = closed
	}
	if err != nil {
		return nil, fmt.Errorf("recording the daemon's places: %w", err)
	}
	if err := os.Link(tmp.Name(), d.placesFile()); err != nil && !errors.Is(err, fs.ErrExist) {
		return nil, fmt.Errorf("recording the daemon's places: %w", err)
	}
	return d.Places()
}
