pub fn prepare_payload(payload: &mut [u8], value: u64) {
    if payload.is_empty() {
        return;
    }
    let bytes = value.to_le_bytes();
    let copy = bytes.len().min(payload.len());
    payload[..copy].copy_from_slice(&bytes[..copy]);
    let tag = (value >> 56) as u8;
    for (index, byte) in payload[copy..].iter_mut().enumerate() {
        *byte = tag
            .wrapping_mul(31)
            .wrapping_add((index as u8).wrapping_mul(17))
            .wrapping_add(0x5d);
    }
}

pub fn encode_key(slot: u64, max_key_size: usize) -> Vec<u8> {
    let mut key = vec![0_u8; max_key_size.max(1)];
    prepare_payload(&mut key, slot);
    key
}

#[cfg(test)]
mod tests {
    use super::prepare_payload;

    #[test]
    fn payload_fills_beyond_prefix() {
        let mut payload = vec![0_u8; 16];
        prepare_payload(&mut payload, (3_u64 << 56) | 42);
        assert_eq!(&payload[..8], &((3_u64 << 56) | 42).to_le_bytes());
        assert_ne!(payload[8], 0);
    }
}
