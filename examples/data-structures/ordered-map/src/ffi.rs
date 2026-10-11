// Pointer validity and lifecycle requirements are defined by CANDIDATE_CONTRACT.md.
#![allow(clippy::missing_safety_doc)]

use super::*;
use std::slice;

#[no_mangle]
pub extern "C" fn vsom_abi_version() -> u32 {
    ABI_VERSION
}

#[no_mangle]
pub unsafe extern "C" fn vsom_map_create(
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
pub unsafe extern "C" fn vsom_map_destroy(map: *mut Map) {
    if !map.is_null() {
        drop(unsafe { Box::from_raw(map) });
    }
}

#[no_mangle]
pub unsafe extern "C" fn vsom_client_create(
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
pub unsafe extern "C" fn vsom_client_destroy(client: *mut Client) {
    if !client.is_null() {
        drop(unsafe { Box::from_raw(client) });
    }
}

unsafe fn borrowed(ptr: *const u8, length: u64) -> Result<&'static [u8], u32> {
    let Ok(length) = usize::try_from(length) else {
        return Err(STATUS_INVALID);
    };
    if length == 0 {
        return Ok(&[]);
    }
    if ptr.is_null() {
        return Err(STATUS_INVALID);
    }
    Ok(unsafe { slice::from_raw_parts(ptr, length) })
}

unsafe fn borrowed_mut(ptr: *mut u8, length: u64) -> Result<&'static mut [u8], u32> {
    let Ok(length) = usize::try_from(length) else {
        return Err(STATUS_INVALID);
    };
    if length == 0 {
        return Ok(&mut []);
    }
    if ptr.is_null() {
        return Err(STATUS_INVALID);
    }
    Ok(unsafe { slice::from_raw_parts_mut(ptr, length) })
}

#[no_mangle]
pub unsafe extern "C" fn vsom_try_put(
    client: *mut Client,
    key: *const u8,
    key_len: u64,
    value: *const u8,
    value_len: u64,
) -> u32 {
    let Some(client) = (unsafe { client.as_ref() }) else {
        return STATUS_INVALID;
    };
    let key = match unsafe { borrowed(key, key_len) } {
        Ok(key) => key,
        Err(status) => return status,
    };
    let value = match unsafe { borrowed(value, value_len) } {
        Ok(value) => value,
        Err(status) => return status,
    };
    client.try_put(key, value)
}

fn write_len(output: *mut u64, length: usize) -> u32 {
    if output.is_null() {
        return STATUS_INVALID;
    }
    unsafe { output.write(length as u64) };
    STATUS_OK
}

#[no_mangle]
pub unsafe extern "C" fn vsom_try_get(
    client: *mut Client,
    key: *const u8,
    key_len: u64,
    value_out: *mut u8,
    value_cap: u64,
    value_len: *mut u64,
) -> u32 {
    let Some(client) = (unsafe { client.as_ref() }) else {
        return STATUS_INVALID;
    };
    if value_len.is_null() {
        return STATUS_INVALID;
    }
    let key = match unsafe { borrowed(key, key_len) } {
        Ok(key) => key,
        Err(status) => return status,
    };
    let output = match unsafe { borrowed_mut(value_out, value_cap) } {
        Ok(output) => output,
        Err(status) => return status,
    };
    match client.try_get(key, output) {
        Ok(length) => write_len(value_len, length),
        Err(status) => status,
    }
}

#[no_mangle]
pub unsafe extern "C" fn vsom_try_remove(
    client: *mut Client,
    key: *const u8,
    key_len: u64,
    value_out: *mut u8,
    value_cap: u64,
    value_len: *mut u64,
) -> u32 {
    let Some(client) = (unsafe { client.as_ref() }) else {
        return STATUS_INVALID;
    };
    if value_len.is_null() {
        return STATUS_INVALID;
    }
    let key = match unsafe { borrowed(key, key_len) } {
        Ok(key) => key,
        Err(status) => return status,
    };
    let output = match unsafe { borrowed_mut(value_out, value_cap) } {
        Ok(output) => output,
        Err(status) => return status,
    };
    match client.try_remove(key, output) {
        Ok(length) => write_len(value_len, length),
        Err(status) => status,
    }
}

unsafe fn copy_kv_status(
    result: Result<(usize, usize), u32>,
    key_len: *mut u64,
    val_len: *mut u64,
) -> u32 {
    match result {
        Ok((key_length, val_length)) => {
            if key_len.is_null() || val_len.is_null() {
                return STATUS_INVALID;
            }
            unsafe {
                key_len.write(key_length as u64);
                val_len.write(val_length as u64);
            }
            STATUS_OK
        }
        Err(status) => status,
    }
}

#[no_mangle]
pub unsafe extern "C" fn vsom_try_min(
    client: *mut Client,
    key_out: *mut u8,
    key_cap: u64,
    key_len: *mut u64,
    val_out: *mut u8,
    val_cap: u64,
    val_len: *mut u64,
) -> u32 {
    let Some(client) = (unsafe { client.as_ref() }) else {
        return STATUS_INVALID;
    };
    if key_len.is_null() || val_len.is_null() {
        return STATUS_INVALID;
    }
    let key_out = match unsafe { borrowed_mut(key_out, key_cap) } {
        Ok(output) => output,
        Err(status) => return status,
    };
    let val_out = match unsafe { borrowed_mut(val_out, val_cap) } {
        Ok(output) => output,
        Err(status) => return status,
    };
    unsafe { copy_kv_status(client.try_min(key_out, val_out), key_len, val_len) }
}

#[no_mangle]
pub unsafe extern "C" fn vsom_try_max(
    client: *mut Client,
    key_out: *mut u8,
    key_cap: u64,
    key_len: *mut u64,
    val_out: *mut u8,
    val_cap: u64,
    val_len: *mut u64,
) -> u32 {
    let Some(client) = (unsafe { client.as_ref() }) else {
        return STATUS_INVALID;
    };
    if key_len.is_null() || val_len.is_null() {
        return STATUS_INVALID;
    }
    let key_out = match unsafe { borrowed_mut(key_out, key_cap) } {
        Ok(output) => output,
        Err(status) => return status,
    };
    let val_out = match unsafe { borrowed_mut(val_out, val_cap) } {
        Ok(output) => output,
        Err(status) => return status,
    };
    unsafe { copy_kv_status(client.try_max(key_out, val_out), key_len, val_len) }
}

#[no_mangle]
pub unsafe extern "C" fn vsom_try_predecessor(
    client: *mut Client,
    key: *const u8,
    key_len: u64,
    key_out: *mut u8,
    key_cap: u64,
    key_len_out: *mut u64,
    val_out: *mut u8,
    val_cap: u64,
    val_len: *mut u64,
) -> u32 {
    let Some(client) = (unsafe { client.as_ref() }) else {
        return STATUS_INVALID;
    };
    if key_len_out.is_null() || val_len.is_null() {
        return STATUS_INVALID;
    }
    let key = match unsafe { borrowed(key, key_len) } {
        Ok(key) => key,
        Err(status) => return status,
    };
    let key_out = match unsafe { borrowed_mut(key_out, key_cap) } {
        Ok(output) => output,
        Err(status) => return status,
    };
    let val_out = match unsafe { borrowed_mut(val_out, val_cap) } {
        Ok(output) => output,
        Err(status) => return status,
    };
    unsafe {
        copy_kv_status(
            client.try_predecessor(key, key_out, val_out),
            key_len_out,
            val_len,
        )
    }
}

#[no_mangle]
pub unsafe extern "C" fn vsom_try_successor(
    client: *mut Client,
    key: *const u8,
    key_len: u64,
    key_out: *mut u8,
    key_cap: u64,
    key_len_out: *mut u64,
    val_out: *mut u8,
    val_cap: u64,
    val_len: *mut u64,
) -> u32 {
    let Some(client) = (unsafe { client.as_ref() }) else {
        return STATUS_INVALID;
    };
    if key_len_out.is_null() || val_len.is_null() {
        return STATUS_INVALID;
    }
    let key = match unsafe { borrowed(key, key_len) } {
        Ok(key) => key,
        Err(status) => return status,
    };
    let key_out = match unsafe { borrowed_mut(key_out, key_cap) } {
        Ok(output) => output,
        Err(status) => return status,
    };
    let val_out = match unsafe { borrowed_mut(val_out, val_cap) } {
        Ok(output) => output,
        Err(status) => return status,
    };
    unsafe {
        copy_kv_status(
            client.try_successor(key, key_out, val_out),
            key_len_out,
            val_len,
        )
    }
}

#[no_mangle]
pub unsafe extern "C" fn vsom_try_range(
    client: *mut Client,
    start: *const u8,
    start_len: u64,
    end: *const u8,
    end_len: u64,
    keys_out: *mut u8,
    key_stride: u64,
    vals_out: *mut u8,
    val_stride: u64,
    lengths_out_key: *mut u64,
    lengths_out_val: *mut u64,
    max_items: u64,
    count_out: *mut u64,
    remaining_out: *mut u64,
) -> u32 {
    let Some(client) = (unsafe { client.as_ref() }) else {
        return STATUS_INVALID;
    };
    if count_out.is_null() || remaining_out.is_null() {
        return STATUS_INVALID;
    }
    let start = match unsafe { borrowed(start, start_len) } {
        Ok(start) => start,
        Err(status) => return status,
    };
    let end = match unsafe { borrowed(end, end_len) } {
        Ok(end) => end,
        Err(status) => return status,
    };
    let Ok(max_items) = usize::try_from(max_items) else {
        return STATUS_INVALID;
    };
    let Ok(key_stride) = usize::try_from(key_stride) else {
        return STATUS_INVALID;
    };
    let Ok(val_stride) = usize::try_from(val_stride) else {
        return STATUS_INVALID;
    };
    let Some(keys_bytes) = key_stride.checked_mul(max_items) else {
        return STATUS_INVALID;
    };
    let Some(vals_bytes) = val_stride.checked_mul(max_items) else {
        return STATUS_INVALID;
    };
    let keys_out = match unsafe { borrowed_mut(keys_out, keys_bytes as u64) } {
        Ok(output) => output,
        Err(status) => return status,
    };
    let vals_out = match unsafe { borrowed_mut(vals_out, vals_bytes as u64) } {
        Ok(output) => output,
        Err(status) => return status,
    };
    let lengths_key = if max_items == 0 {
        &mut [][..]
    } else if lengths_out_key.is_null() {
        return STATUS_INVALID;
    } else {
        unsafe { slice::from_raw_parts_mut(lengths_out_key, max_items) }
    };
    let lengths_val = if max_items == 0 {
        &mut [][..]
    } else if lengths_out_val.is_null() {
        return STATUS_INVALID;
    } else {
        unsafe { slice::from_raw_parts_mut(lengths_out_val, max_items) }
    };
    match client.try_range(
        start,
        end,
        keys_out,
        key_stride,
        vals_out,
        val_stride,
        lengths_key,
        lengths_val,
        max_items,
    ) {
        Ok((count, remaining)) => {
            unsafe {
                count_out.write(count);
                remaining_out.write(remaining);
            }
            STATUS_OK
        }
        Err(status) => status,
    }
}
