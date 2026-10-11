// Pointer validity and lifecycle requirements are defined by CANDIDATE_CONTRACT.md.
#![allow(clippy::missing_safety_doc)]

use super::*;
use std::slice;

#[no_mangle]
pub extern "C" fn vsum_abi_version() -> u32 {
    ABI_VERSION
}

#[no_mangle]
pub unsafe extern "C" fn vsum_map_create(
    max_key_size: u64,
    max_value_size: u64,
    client_count: u32,
    map_out: *mut *mut Map,
) -> u32 {
    if map_out.is_null() {
        return STATUS_INVALID;
    }
    let map = match Map::new(max_key_size, max_value_size, client_count) {
        Ok(map) => map,
        Err(status) => return status,
    };

    unsafe { map_out.write(Box::into_raw(Box::new(map))) };
    STATUS_OK
}

#[no_mangle]
pub unsafe extern "C" fn vsum_map_destroy(map: *mut Map) {
    if !map.is_null() {
        drop(unsafe { Box::from_raw(map) });
    }
}

#[no_mangle]
pub unsafe extern "C" fn vsum_client_create(
    map: *mut Map,
    client_id: u32,
    client_out: *mut *mut Client,
) -> u32 {
    let Some(map) = (unsafe { map.as_ref() }) else {
        return STATUS_INVALID;
    };
    if client_out.is_null() {
        return STATUS_INVALID;
    }
    let client = match map.client(client_id) {
        Ok(client) => client,
        Err(status) => return status,
    };

    unsafe { client_out.write(Box::into_raw(Box::new(client))) };
    STATUS_OK
}

#[no_mangle]
pub unsafe extern "C" fn vsum_client_destroy(client: *mut Client) {
    if !client.is_null() {
        drop(unsafe { Box::from_raw(client) });
    }
}

#[no_mangle]
pub unsafe extern "C" fn vsum_try_put(
    client: *mut Client,
    key: *const u8,
    key_len: u64,
    value: *const u8,
    value_len: u64,
) -> u32 {
    let Some(client) = (unsafe { client.as_ref() }) else {
        return STATUS_INVALID;
    };
    let Ok(key_len) = usize::try_from(key_len) else {
        return STATUS_INVALID;
    };
    let Ok(value_len) = usize::try_from(value_len) else {
        return STATUS_INVALID;
    };
    let key = match borrow_input(key, key_len) {
        Ok(key) => key,
        Err(status) => return status,
    };
    let value = match borrow_input(value, value_len) {
        Ok(value) => value,
        Err(status) => return status,
    };

    client.try_put(key, value)
}

#[no_mangle]
pub unsafe extern "C" fn vsum_try_get(
    client: *mut Client,
    key: *const u8,
    key_len: u64,
    output: *mut u8,
    output_capacity: u64,
    output_length: *mut u64,
) -> u32 {
    unsafe {
        lookup(
            client,
            key,
            key_len,
            output,
            output_capacity,
            output_length,
            false,
        )
    }
}

#[no_mangle]
pub unsafe extern "C" fn vsum_try_remove(
    client: *mut Client,
    key: *const u8,
    key_len: u64,
    output: *mut u8,
    output_capacity: u64,
    output_length: *mut u64,
) -> u32 {
    unsafe {
        lookup(
            client,
            key,
            key_len,
            output,
            output_capacity,
            output_length,
            true,
        )
    }
}

unsafe fn lookup(
    client: *mut Client,
    key: *const u8,
    key_len: u64,
    output: *mut u8,
    output_capacity: u64,
    output_length: *mut u64,
    remove: bool,
) -> u32 {
    let Some(client) = (unsafe { client.as_ref() }) else {
        return STATUS_INVALID;
    };
    if output_length.is_null() {
        return STATUS_INVALID;
    }
    let Ok(key_len) = usize::try_from(key_len) else {
        return STATUS_INVALID;
    };
    let Ok(output_capacity) = usize::try_from(output_capacity) else {
        return STATUS_INVALID;
    };
    let key = match borrow_input(key, key_len) {
        Ok(key) => key,
        Err(status) => return status,
    };
    let output = match borrow_output(output, output_capacity) {
        Ok(output) => output,
        Err(status) => return status,
    };

    let result = if remove {
        client.try_remove(key, output)
    } else {
        client.try_get(key, output)
    };
    match result {
        Ok(length) => {
            unsafe { output_length.write(length as u64) };
            STATUS_OK
        }
        Err(status) => status,
    }
}

fn borrow_input<'a>(pointer: *const u8, length: usize) -> Result<&'a [u8], u32> {
    if length == 0 {
        Ok(&[])
    } else if pointer.is_null() {
        Err(STATUS_INVALID)
    } else {
        Ok(unsafe { slice::from_raw_parts(pointer, length) })
    }
}

fn borrow_output<'a>(pointer: *mut u8, capacity: usize) -> Result<&'a mut [u8], u32> {
    if capacity == 0 {
        Ok(&mut [])
    } else if pointer.is_null() {
        Err(STATUS_INVALID)
    } else {
        Ok(unsafe { slice::from_raw_parts_mut(pointer, capacity) })
    }
}
