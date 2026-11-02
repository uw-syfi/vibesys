//go:build !unix

package store

import "errors"

func lockDir(string) (func(), error) {
	return nil, errors.New("store: the file store runs on Linux and macOS")
}
