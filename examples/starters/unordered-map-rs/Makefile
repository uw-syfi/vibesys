.PHONY: all clean unordered-map-candidate.so

TARGET_DIR := $(or $(CARGO_TARGET_DIR),target)

all: unordered-map-candidate.so

unordered-map-candidate.so: Cargo.toml Cargo.lock src/lib.rs src/ffi.rs
	cargo build --release --locked
	cp "$(TARGET_DIR)/release/libunordered_map_candidate.$$(test "$$(uname -s)" = Darwin && printf dylib || printf so)" "$@"

clean:
	cargo clean
	rm -f unordered-map-candidate.so
