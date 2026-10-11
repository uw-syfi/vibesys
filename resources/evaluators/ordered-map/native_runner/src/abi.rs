use std::collections::BTreeMap;
use std::ffi::{c_char, c_int, c_void, CStr, CString};
use std::ops::Bound::{Excluded, Included, Unbounded};
use std::path::Path;
use std::sync::{Arc, Mutex};

pub const ABI_VERSION: u32 = 1;
pub const STATUS_OK: u32 = 0;
pub const STATUS_MISSING: u32 = 1;
pub const STATUS_INVALID: u32 = 2;
pub const STATUS_INTERNAL_ERROR: u32 = 3;

pub type MapHandle = *mut c_void;
pub type ClientHandle = *mut c_void;

type AbiVersionFn = unsafe extern "C" fn() -> u32;
type MapCreateFn = unsafe extern "C" fn(u64, u64, u32, *mut MapHandle) -> u32;
type MapDestroyFn = unsafe extern "C" fn(MapHandle);
type ClientCreateFn = unsafe extern "C" fn(MapHandle, u32, *mut ClientHandle) -> u32;
type ClientDestroyFn = unsafe extern "C" fn(ClientHandle);
type PutFn = unsafe extern "C" fn(ClientHandle, *const u8, u64, *const u8, u64) -> u32;
type GetFn = unsafe extern "C" fn(ClientHandle, *const u8, u64, *mut u8, u64, *mut u64) -> u32;
type OrderedFn =
    unsafe extern "C" fn(ClientHandle, *mut u8, u64, *mut u64, *mut u8, u64, *mut u64) -> u32;
type NeighborFn = unsafe extern "C" fn(
    ClientHandle,
    *const u8,
    u64,
    *mut u8,
    u64,
    *mut u64,
    *mut u8,
    u64,
    *mut u64,
) -> u32;
type RangeFn = unsafe extern "C" fn(
    ClientHandle,
    *const u8,
    u64,
    *const u8,
    u64,
    *mut u8,
    u64,
    *mut u8,
    u64,
    *mut u64,
    *mut u64,
    u64,
    *mut u64,
    *mut u64,
) -> u32;

#[derive(Clone)]
pub struct Api {
    _library: Option<Arc<DynamicLibrary>>,
    map_create: MapCreateFn,
    map_destroy: MapDestroyFn,
    client_create: ClientCreateFn,
    client_destroy: ClientDestroyFn,
    put: PutFn,
    get: GetFn,
    remove: GetFn,
    min: OrderedFn,
    max: OrderedFn,
    predecessor: NeighborFn,
    successor: NeighborFn,
    range: RangeFn,
}

pub struct Map {
    api: Api,
    handle: MapHandle,
}

unsafe impl Send for Map {}
unsafe impl Sync for Map {}

pub struct Client {
    api: Api,
    handle: ClientHandle,
}

unsafe impl Send for Client {}

impl Api {
    pub fn load(path: &Path) -> Result<Self, String> {
        let library = Arc::new(DynamicLibrary::open(path)?);
        unsafe {
            let abi_version: AbiVersionFn = library.symbol(b"vsom_abi_version\0")?;
            let version = abi_version();
            if version != ABI_VERSION {
                return Err(format!(
                    "candidate ABI version {version}, expected {ABI_VERSION}"
                ));
            }
            Ok(Self {
                _library: Some(library.clone()),
                map_create: library.symbol(b"vsom_map_create\0")?,
                map_destroy: library.symbol(b"vsom_map_destroy\0")?,
                client_create: library.symbol(b"vsom_client_create\0")?,
                client_destroy: library.symbol(b"vsom_client_destroy\0")?,
                put: library.symbol(b"vsom_try_put\0")?,
                get: library.symbol(b"vsom_try_get\0")?,
                remove: library.symbol(b"vsom_try_remove\0")?,
                min: library.symbol(b"vsom_try_min\0")?,
                max: library.symbol(b"vsom_try_max\0")?,
                predecessor: library.symbol(b"vsom_try_predecessor\0")?,
                successor: library.symbol(b"vsom_try_successor\0")?,
                range: library.symbol(b"vsom_try_range\0")?,
            })
        }
    }

    pub fn reference() -> Self {
        Self {
            _library: None,
            map_create: reference_map_create,
            map_destroy: reference_map_destroy,
            client_create: reference_client_create,
            client_destroy: reference_client_destroy,
            put: reference_put,
            get: reference_get,
            remove: reference_remove,
            min: reference_min,
            max: reference_max,
            predecessor: reference_predecessor,
            successor: reference_successor,
            range: reference_range,
        }
    }

    pub fn create_map(
        &self,
        max_key_size: u64,
        max_value_size: u64,
        client_count: u32,
    ) -> Result<Map, String> {
        let mut handle = std::ptr::null_mut();
        let status =
            unsafe { (self.map_create)(max_key_size, max_value_size, client_count, &mut handle) };
        if status != STATUS_OK || handle.is_null() {
            return Err(format!("vsom_map_create returned status {status}"));
        }
        Ok(Map {
            api: self.clone(),
            handle,
        })
    }
}

impl Map {
    pub fn create_client(&self, id: u32) -> Result<Client, String> {
        let mut handle = std::ptr::null_mut();
        let status = unsafe { (self.api.client_create)(self.handle, id, &mut handle) };
        if status != STATUS_OK || handle.is_null() {
            return Err(format!("vsom_client_create({id}) returned status {status}"));
        }
        Ok(Client {
            api: self.api.clone(),
            handle,
        })
    }
}

impl Drop for Map {
    fn drop(&mut self) {
        unsafe { (self.api.map_destroy)(self.handle) };
    }
}

impl Client {
    pub fn put(&mut self, key: &[u8], value: &[u8]) -> u32 {
        unsafe {
            (self.api.put)(
                self.handle,
                key.as_ptr(),
                key.len() as u64,
                value.as_ptr(),
                value.len() as u64,
            )
        }
    }

    pub fn get_raw(
        &mut self,
        key: &[u8],
        output: *mut u8,
        output_capacity: u64,
        output_length: &mut u64,
    ) -> u32 {
        unsafe {
            (self.api.get)(
                self.handle,
                key.as_ptr(),
                key.len() as u64,
                output,
                output_capacity,
                output_length as *mut u64,
            )
        }
    }

    pub fn remove_raw(
        &mut self,
        key: &[u8],
        output: *mut u8,
        output_capacity: u64,
        output_length: &mut u64,
    ) -> u32 {
        unsafe {
            (self.api.remove)(
                self.handle,
                key.as_ptr(),
                key.len() as u64,
                output,
                output_capacity,
                output_length as *mut u64,
            )
        }
    }

    pub fn lookup(
        &mut self,
        remove: bool,
        key: &[u8],
        output: &mut [u8],
    ) -> Result<(u32, usize), String> {
        let mut length = u64::MAX;
        let status = if remove {
            self.remove_raw(key, output.as_mut_ptr(), output.len() as u64, &mut length)
        } else {
            self.get_raw(key, output.as_mut_ptr(), output.len() as u64, &mut length)
        };
        decode_optional_length(status, length, output.len())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn ordered_raw(
        &mut self,
        max: bool,
        key_out: *mut u8,
        key_cap: u64,
        key_len: &mut u64,
        val_out: *mut u8,
        val_cap: u64,
        val_len: &mut u64,
    ) -> u32 {
        let function = if max { self.api.max } else { self.api.min };
        unsafe {
            function(
                self.handle,
                key_out,
                key_cap,
                key_len as *mut u64,
                val_out,
                val_cap,
                val_len as *mut u64,
            )
        }
    }

    #[allow(clippy::too_many_arguments)]
    pub fn neighbor_raw(
        &mut self,
        successor: bool,
        key: &[u8],
        key_out: *mut u8,
        key_cap: u64,
        key_len: &mut u64,
        val_out: *mut u8,
        val_cap: u64,
        val_len: &mut u64,
    ) -> u32 {
        let function = if successor {
            self.api.successor
        } else {
            self.api.predecessor
        };
        unsafe {
            function(
                self.handle,
                key.as_ptr(),
                key.len() as u64,
                key_out,
                key_cap,
                key_len as *mut u64,
                val_out,
                val_cap,
                val_len as *mut u64,
            )
        }
    }

    #[allow(clippy::too_many_arguments)]
    pub fn range_raw(
        &mut self,
        start: &[u8],
        end: &[u8],
        keys_out: *mut u8,
        key_stride: u64,
        vals_out: *mut u8,
        val_stride: u64,
        lengths_key: *mut u64,
        lengths_val: *mut u64,
        max_items: u64,
        count: &mut u64,
        remaining: &mut u64,
    ) -> u32 {
        unsafe {
            (self.api.range)(
                self.handle,
                start.as_ptr(),
                start.len() as u64,
                end.as_ptr(),
                end.len() as u64,
                keys_out,
                key_stride,
                vals_out,
                val_stride,
                lengths_key,
                lengths_val,
                max_items,
                count as *mut u64,
                remaining as *mut u64,
            )
        }
    }
}

impl Drop for Client {
    fn drop(&mut self) {
        unsafe { (self.api.client_destroy)(self.handle) };
    }
}

fn decode_optional_length(
    status: u32,
    length: u64,
    capacity: usize,
) -> Result<(u32, usize), String> {
    if status == STATUS_MISSING || status == STATUS_INVALID {
        if length != u64::MAX {
            return Err("candidate modified output length for a non-OK lookup".to_string());
        }
        return Ok((status, 0));
    }
    if length > capacity as u64 {
        return Err(format!(
            "candidate returned length {length} larger than output capacity {capacity}"
        ));
    }
    Ok((status, length as usize))
}

struct ReferenceMap {
    max_key_size: usize,
    max_value_size: usize,
    client_count: u32,
    entries: Mutex<BTreeMap<Vec<u8>, Vec<u8>>>,
}

struct ReferenceClient {
    map: *mut ReferenceMap,
}

fn slice_in(data: *const u8, length: u64) -> Result<&'static [u8], u32> {
    let Ok(length) = usize::try_from(length) else {
        return Err(STATUS_INVALID);
    };
    if length == 0 {
        return Ok(&[]);
    }
    if data.is_null() {
        return Err(STATUS_INVALID);
    }
    Ok(unsafe { std::slice::from_raw_parts(data, length) })
}

fn copy_out(src: &[u8], dst: *mut u8, cap: u64) -> Result<(), u32> {
    if src.len() > cap as usize || (dst.is_null() && !src.is_empty()) {
        return Err(STATUS_INVALID);
    }
    if !src.is_empty() {
        unsafe { std::ptr::copy_nonoverlapping(src.as_ptr(), dst, src.len()) };
    }
    Ok(())
}

unsafe extern "C" fn reference_map_create(
    max_key_size: u64,
    max_value_size: u64,
    client_count: u32,
    output: *mut MapHandle,
) -> u32 {
    if max_key_size == 0 || max_value_size == 0 || client_count == 0 || output.is_null() {
        return STATUS_INVALID;
    }
    let map = Box::new(ReferenceMap {
        max_key_size: max_key_size as usize,
        max_value_size: max_value_size as usize,
        client_count,
        entries: Mutex::new(BTreeMap::new()),
    });
    unsafe { *output = Box::into_raw(map).cast() };
    STATUS_OK
}

unsafe extern "C" fn reference_map_destroy(map: MapHandle) {
    if !map.is_null() {
        drop(unsafe { Box::from_raw(map.cast::<ReferenceMap>()) });
    }
}

unsafe extern "C" fn reference_client_create(
    map: MapHandle,
    id: u32,
    output: *mut ClientHandle,
) -> u32 {
    if map.is_null() || output.is_null() {
        return STATUS_INVALID;
    }
    let map_ref = unsafe { &*map.cast::<ReferenceMap>() };
    if id >= map_ref.client_count {
        return STATUS_INVALID;
    }
    let client = Box::new(ReferenceClient { map: map.cast() });
    unsafe { *output = Box::into_raw(client).cast() };
    STATUS_OK
}

unsafe extern "C" fn reference_client_destroy(client: ClientHandle) {
    if !client.is_null() {
        drop(unsafe { Box::from_raw(client.cast::<ReferenceClient>()) });
    }
}

fn client_map(client: ClientHandle) -> Result<&'static ReferenceMap, u32> {
    if client.is_null() {
        return Err(STATUS_INVALID);
    }
    let client = unsafe { &*client.cast::<ReferenceClient>() };
    if client.map.is_null() {
        return Err(STATUS_INVALID);
    }
    Ok(unsafe { &*client.map })
}

unsafe extern "C" fn reference_put(
    client: ClientHandle,
    key: *const u8,
    key_len: u64,
    value: *const u8,
    value_len: u64,
) -> u32 {
    let map = match client_map(client) {
        Ok(map) => map,
        Err(status) => return status,
    };
    let key = match slice_in(key, key_len) {
        Ok(key) => key,
        Err(status) => return status,
    };
    let value = match slice_in(value, value_len) {
        Ok(value) => value,
        Err(status) => return status,
    };
    if key.len() > map.max_key_size || value.len() > map.max_value_size {
        return STATUS_INVALID;
    }
    let mut entries = match map.entries.lock() {
        Ok(entries) => entries,
        Err(_) => return STATUS_INTERNAL_ERROR,
    };
    entries.insert(key.to_vec(), value.to_vec());
    STATUS_OK
}

fn reference_lookup(
    client: ClientHandle,
    key: *const u8,
    key_len: u64,
    output: *mut u8,
    output_capacity: u64,
    output_length: *mut u64,
    remove: bool,
) -> u32 {
    if output_length.is_null() {
        return STATUS_INVALID;
    }
    let map = match client_map(client) {
        Ok(map) => map,
        Err(status) => return status,
    };
    let key = match slice_in(key, key_len) {
        Ok(key) => key,
        Err(status) => return status,
    };
    if key.len() > map.max_key_size {
        return STATUS_INVALID;
    }
    let mut entries = match map.entries.lock() {
        Ok(entries) => entries,
        Err(_) => return STATUS_INTERNAL_ERROR,
    };
    let Some(value) = entries.get(key) else {
        return STATUS_MISSING;
    };
    if copy_out(value, output, output_capacity).is_err() {
        return STATUS_INVALID;
    }
    let length = value.len() as u64;
    if remove {
        entries.remove(key);
    }
    unsafe { *output_length = length };
    STATUS_OK
}

unsafe extern "C" fn reference_get(
    client: ClientHandle,
    key: *const u8,
    key_len: u64,
    output: *mut u8,
    output_capacity: u64,
    output_length: *mut u64,
) -> u32 {
    reference_lookup(
        client,
        key,
        key_len,
        output,
        output_capacity,
        output_length,
        false,
    )
}

unsafe extern "C" fn reference_remove(
    client: ClientHandle,
    key: *const u8,
    key_len: u64,
    output: *mut u8,
    output_capacity: u64,
    output_length: *mut u64,
) -> u32 {
    reference_lookup(
        client,
        key,
        key_len,
        output,
        output_capacity,
        output_length,
        true,
    )
}

#[allow(clippy::too_many_arguments)]
fn write_pair(
    key: &[u8],
    value: &[u8],
    key_out: *mut u8,
    key_cap: u64,
    key_len: *mut u64,
    val_out: *mut u8,
    val_cap: u64,
    val_len: *mut u64,
) -> u32 {
    if key_len.is_null() || val_len.is_null() {
        return STATUS_INVALID;
    }
    if copy_out(key, key_out, key_cap).is_err() || copy_out(value, val_out, val_cap).is_err() {
        return STATUS_INVALID;
    }
    unsafe {
        *key_len = key.len() as u64;
        *val_len = value.len() as u64;
    }
    STATUS_OK
}

fn reference_endpoint(client: ClientHandle, max: bool, args: OrderedOut) -> u32 {
    let map = match client_map(client) {
        Ok(map) => map,
        Err(status) => return status,
    };
    let entries = match map.entries.lock() {
        Ok(entries) => entries,
        Err(_) => return STATUS_INTERNAL_ERROR,
    };
    let Some((key, value)) = (if max {
        entries.iter().next_back()
    } else {
        entries.iter().next()
    }) else {
        return STATUS_MISSING;
    };
    write_pair(
        key,
        value,
        args.key_out,
        args.key_cap,
        args.key_len,
        args.val_out,
        args.val_cap,
        args.val_len,
    )
}

struct OrderedOut {
    key_out: *mut u8,
    key_cap: u64,
    key_len: *mut u64,
    val_out: *mut u8,
    val_cap: u64,
    val_len: *mut u64,
}

unsafe extern "C" fn reference_min(
    client: ClientHandle,
    key_out: *mut u8,
    key_cap: u64,
    key_len: *mut u64,
    val_out: *mut u8,
    val_cap: u64,
    val_len: *mut u64,
) -> u32 {
    reference_endpoint(
        client,
        false,
        OrderedOut {
            key_out,
            key_cap,
            key_len,
            val_out,
            val_cap,
            val_len,
        },
    )
}

unsafe extern "C" fn reference_max(
    client: ClientHandle,
    key_out: *mut u8,
    key_cap: u64,
    key_len: *mut u64,
    val_out: *mut u8,
    val_cap: u64,
    val_len: *mut u64,
) -> u32 {
    reference_endpoint(
        client,
        true,
        OrderedOut {
            key_out,
            key_cap,
            key_len,
            val_out,
            val_cap,
            val_len,
        },
    )
}

fn reference_neighbor(
    client: ClientHandle,
    key: *const u8,
    key_len: u64,
    successor: bool,
    args: OrderedOut,
) -> u32 {
    let map = match client_map(client) {
        Ok(map) => map,
        Err(status) => return status,
    };
    let query = match slice_in(key, key_len) {
        Ok(key) => key,
        Err(status) => return status,
    };
    if query.len() > map.max_key_size {
        return STATUS_INVALID;
    }
    let entries = match map.entries.lock() {
        Ok(entries) => entries,
        Err(_) => return STATUS_INTERNAL_ERROR,
    };
    let pair = if successor {
        entries
            .range::<[u8], _>((Excluded(query), Unbounded))
            .next()
    } else {
        entries
            .range::<[u8], _>((Unbounded, Excluded(query)))
            .next_back()
    };
    let Some((found, value)) = pair else {
        return STATUS_MISSING;
    };
    write_pair(
        found,
        value,
        args.key_out,
        args.key_cap,
        args.key_len,
        args.val_out,
        args.val_cap,
        args.val_len,
    )
}

unsafe extern "C" fn reference_predecessor(
    client: ClientHandle,
    key: *const u8,
    key_len: u64,
    key_out: *mut u8,
    key_cap: u64,
    key_len_out: *mut u64,
    val_out: *mut u8,
    val_cap: u64,
    val_len: *mut u64,
) -> u32 {
    reference_neighbor(
        client,
        key,
        key_len,
        false,
        OrderedOut {
            key_out,
            key_cap,
            key_len: key_len_out,
            val_out,
            val_cap,
            val_len,
        },
    )
}

unsafe extern "C" fn reference_successor(
    client: ClientHandle,
    key: *const u8,
    key_len: u64,
    key_out: *mut u8,
    key_cap: u64,
    key_len_out: *mut u64,
    val_out: *mut u8,
    val_cap: u64,
    val_len: *mut u64,
) -> u32 {
    reference_neighbor(
        client,
        key,
        key_len,
        true,
        OrderedOut {
            key_out,
            key_cap,
            key_len: key_len_out,
            val_out,
            val_cap,
            val_len,
        },
    )
}

unsafe extern "C" fn reference_range(
    client: ClientHandle,
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
    if count_out.is_null() || remaining_out.is_null() {
        return STATUS_INVALID;
    }
    let map = match client_map(client) {
        Ok(map) => map,
        Err(status) => return status,
    };
    let start = match slice_in(start, start_len) {
        Ok(start) => start,
        Err(status) => return status,
    };
    let end = match slice_in(end, end_len) {
        Ok(end) => end,
        Err(status) => return status,
    };
    if start.len() > map.max_key_size || end.len() > map.max_key_size {
        return STATUS_INVALID;
    }
    let Ok(max_items_usize) = usize::try_from(max_items) else {
        return STATUS_INVALID;
    };
    let Ok(key_stride) = usize::try_from(key_stride) else {
        return STATUS_INVALID;
    };
    let Ok(val_stride) = usize::try_from(val_stride) else {
        return STATUS_INVALID;
    };
    let entries = match map.entries.lock() {
        Ok(entries) => entries,
        Err(_) => return STATUS_INTERNAL_ERROR,
    };
    if start >= end {
        unsafe {
            *count_out = 0;
            *remaining_out = 0;
        }
        return STATUS_OK;
    }
    let matched: Vec<(&[u8], &[u8])> = entries
        .range::<[u8], _>((Included(start), Excluded(end)))
        .map(|(key, value)| (key.as_slice(), value.as_slice()))
        .collect();
    if max_items_usize == 0 {
        unsafe {
            *count_out = 0;
            *remaining_out = u64::from(!matched.is_empty());
        }
        return STATUS_OK;
    }
    if lengths_out_key.is_null() || lengths_out_val.is_null() {
        return STATUS_INVALID;
    }
    let remaining = u64::from(matched.len() > max_items_usize);
    let take = matched.len().min(max_items_usize);
    for (key, value) in &matched[..take] {
        if key.len() > key_stride || value.len() > val_stride {
            return STATUS_INVALID;
        }
    }
    let Some(keys_bytes) = key_stride.checked_mul(max_items_usize) else {
        return STATUS_INVALID;
    };
    let Some(vals_bytes) = val_stride.checked_mul(max_items_usize) else {
        return STATUS_INVALID;
    };
    if (keys_out.is_null() && keys_bytes != 0) || (vals_out.is_null() && vals_bytes != 0) {
        return STATUS_INVALID;
    }
    for (index, (key, value)) in matched[..take].iter().enumerate() {
        let key_offset = index * key_stride;
        let val_offset = index * val_stride;
        unsafe {
            if !key.is_empty() {
                std::ptr::copy_nonoverlapping(key.as_ptr(), keys_out.add(key_offset), key.len());
            }
            if !value.is_empty() {
                std::ptr::copy_nonoverlapping(
                    value.as_ptr(),
                    vals_out.add(val_offset),
                    value.len(),
                );
            }
            *lengths_out_key.add(index) = key.len() as u64;
            *lengths_out_val.add(index) = value.len() as u64;
        }
    }
    unsafe {
        *count_out = take as u64;
        *remaining_out = remaining;
    }
    STATUS_OK
}

struct DynamicLibrary(*mut c_void);

unsafe impl Send for DynamicLibrary {}
unsafe impl Sync for DynamicLibrary {}

impl DynamicLibrary {
    fn open(path: &Path) -> Result<Self, String> {
        let path = CString::new(path.as_os_str().as_encoded_bytes())
            .map_err(|_| "candidate library path contains a NUL byte".to_string())?;
        unsafe {
            clear_dlerror();
            let handle = dlopen(path.as_ptr(), RTLD_NOW | RTLD_LOCAL);
            if handle.is_null() {
                return Err(format!("load candidate library: {}", current_dlerror()));
            }
            Ok(Self(handle))
        }
    }

    unsafe fn symbol<T: Copy>(&self, name: &[u8]) -> Result<T, String> {
        clear_dlerror();
        let pointer = dlsym(self.0, name.as_ptr().cast());
        let error = dlerror();
        if !error.is_null() {
            return Err(format!(
                "load symbol {}: {}",
                String::from_utf8_lossy(&name[..name.len().saturating_sub(1)]),
                CStr::from_ptr(error).to_string_lossy()
            ));
        }
        if pointer.is_null() || std::mem::size_of::<T>() != std::mem::size_of::<*mut c_void>() {
            return Err("dynamic symbol has an unsupported representation".to_string());
        }
        Ok(std::mem::transmute_copy(&pointer))
    }
}

impl Drop for DynamicLibrary {
    fn drop(&mut self) {
        unsafe {
            dlclose(self.0);
        }
    }
}

const RTLD_NOW: c_int = 2;
#[cfg(target_os = "linux")]
const RTLD_LOCAL: c_int = 0;
#[cfg(target_os = "macos")]
const RTLD_LOCAL: c_int = 4;

#[cfg(target_os = "linux")]
#[link(name = "dl")]
extern "C" {}

extern "C" {
    fn dlopen(path: *const c_char, mode: c_int) -> *mut c_void;
    fn dlsym(handle: *mut c_void, symbol: *const c_char) -> *mut c_void;
    fn dlclose(handle: *mut c_void) -> c_int;
    fn dlerror() -> *const c_char;
}

unsafe fn clear_dlerror() {
    while !dlerror().is_null() {}
}

unsafe fn current_dlerror() -> String {
    let error = dlerror();
    if error.is_null() {
        "unknown dynamic loader error".to_string()
    } else {
        CStr::from_ptr(error).to_string_lossy().into_owned()
    }
}
