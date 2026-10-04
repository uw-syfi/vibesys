use std::collections::HashMap;
use std::ffi::{c_char, c_int, c_void, CStr, CString};
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
type LookupFn = unsafe extern "C" fn(ClientHandle, *const u8, u64, *mut u8, u64, *mut u64) -> u32;

#[derive(Clone)]
pub struct Api {
    _library: Option<Arc<DynamicLibrary>>,
    map_create: MapCreateFn,
    map_destroy: MapDestroyFn,
    client_create: ClientCreateFn,
    client_destroy: ClientDestroyFn,
    put: PutFn,
    get: LookupFn,
    remove: LookupFn,
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
            let abi_version: AbiVersionFn = library.symbol(b"vsum_abi_version\0")?;
            let version = abi_version();
            if version != ABI_VERSION {
                return Err(format!(
                    "candidate ABI version {version}, expected {ABI_VERSION}"
                ));
            }
            Ok(Self {
                _library: Some(library.clone()),
                map_create: library.symbol(b"vsum_map_create\0")?,
                map_destroy: library.symbol(b"vsum_map_destroy\0")?,
                client_create: library.symbol(b"vsum_client_create\0")?,
                client_destroy: library.symbol(b"vsum_client_destroy\0")?,
                put: library.symbol(b"vsum_try_put\0")?,
                get: library.symbol(b"vsum_try_get\0")?,
                remove: library.symbol(b"vsum_try_remove\0")?,
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
        }
    }

    pub fn create_map_raw(
        &self,
        max_key_size: u64,
        max_value_size: u64,
        client_count: u32,
        map_out: *mut MapHandle,
    ) -> u32 {
        unsafe { (self.map_create)(max_key_size, max_value_size, client_count, map_out) }
    }

    pub fn create_map(
        &self,
        max_key_size: u64,
        max_value_size: u64,
        client_count: u32,
    ) -> Result<Map, String> {
        let mut handle = std::ptr::null_mut();
        let status = self.create_map_raw(max_key_size, max_value_size, client_count, &mut handle);
        if status != STATUS_OK || handle.is_null() {
            return Err(format!("vsum_map_create returned status {status}"));
        }
        Ok(Map {
            api: self.clone(),
            handle,
        })
    }
}

impl Map {
    pub fn create_client_raw(&self, id: u32, client_out: *mut ClientHandle) -> u32 {
        unsafe { (self.api.client_create)(self.handle, id, client_out) }
    }

    pub fn create_client(&self, id: u32) -> Result<Client, String> {
        let mut handle = std::ptr::null_mut();
        let status = self.create_client_raw(id, &mut handle);
        if status != STATUS_OK || handle.is_null() {
            return Err(format!("vsum_client_create({id}) returned status {status}"));
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
    pub fn put_raw(
        &mut self,
        key: *const u8,
        key_len: u64,
        value: *const u8,
        value_len: u64,
    ) -> u32 {
        unsafe { (self.api.put)(self.handle, key, key_len, value, value_len) }
    }

    pub fn put(&mut self, key: &[u8], value: &[u8]) -> u32 {
        self.put_raw(
            key.as_ptr(),
            key.len() as u64,
            value.as_ptr(),
            value.len() as u64,
        )
    }

    pub fn get_raw(
        &mut self,
        key: *const u8,
        key_len: u64,
        output: *mut u8,
        output_capacity: u64,
        output_length: *mut u64,
    ) -> u32 {
        unsafe {
            (self.api.get)(
                self.handle,
                key,
                key_len,
                output,
                output_capacity,
                output_length,
            )
        }
    }

    pub fn remove_raw(
        &mut self,
        key: *const u8,
        key_len: u64,
        output: *mut u8,
        output_capacity: u64,
        output_length: *mut u64,
    ) -> u32 {
        unsafe {
            (self.api.remove)(
                self.handle,
                key,
                key_len,
                output,
                output_capacity,
                output_length,
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
            self.remove_raw(
                key.as_ptr(),
                key.len() as u64,
                output.as_mut_ptr(),
                output.len() as u64,
                &mut length,
            )
        } else {
            self.get_raw(
                key.as_ptr(),
                key.len() as u64,
                output.as_mut_ptr(),
                output.len() as u64,
                &mut length,
            )
        };
        if status == STATUS_MISSING || status == STATUS_INVALID {
            if length != u64::MAX {
                return Err(
                    "candidate modified output length for a missing or invalid lookup".to_string(),
                );
            }
            return Ok((status, 0));
        }
        if length > output.len() as u64 {
            return Err(format!(
                "candidate returned length {length} larger than output capacity {}",
                output.len()
            ));
        }
        Ok((status, length as usize))
    }

    pub fn get(&mut self, key: &[u8], output: &mut [u8]) -> Result<(u32, usize), String> {
        self.lookup(false, key, output)
    }

    pub fn remove(&mut self, key: &[u8], output: &mut [u8]) -> Result<(u32, usize), String> {
        self.lookup(true, key, output)
    }
}

impl Drop for Client {
    fn drop(&mut self) {
        unsafe { (self.api.client_destroy)(self.handle) };
    }
}

struct ReferenceMap {
    max_key_size: usize,
    max_value_size: usize,
    client_count: u32,
    values: Mutex<HashMap<Vec<u8>, Vec<u8>>>,
}

struct ReferenceClient {
    map: *mut ReferenceMap,
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
        values: Mutex::new(HashMap::new()),
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

fn copied_input(data: *const u8, length: u64, max_size: usize) -> Result<Vec<u8>, u32> {
    if length as usize > max_size || (data.is_null() && length != 0) {
        return Err(STATUS_INVALID);
    }
    if length == 0 {
        Ok(Vec::new())
    } else {
        Ok(unsafe { std::slice::from_raw_parts(data, length as usize) }.to_vec())
    }
}

unsafe extern "C" fn reference_put(
    client: ClientHandle,
    key: *const u8,
    key_len: u64,
    value: *const u8,
    value_len: u64,
) -> u32 {
    if client.is_null() {
        return STATUS_INVALID;
    }
    let client = unsafe { &*client.cast::<ReferenceClient>() };
    let map = unsafe { &*client.map };
    let key = match copied_input(key, key_len, map.max_key_size) {
        Ok(key) => key,
        Err(status) => return status,
    };
    let value = match copied_input(value, value_len, map.max_value_size) {
        Ok(value) => value,
        Err(status) => return status,
    };
    let mut values = match map.values.lock() {
        Ok(values) => values,
        Err(_) => return STATUS_INTERNAL_ERROR,
    };
    values.insert(key, value);
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
    if client.is_null() || output_length.is_null() {
        return STATUS_INVALID;
    }
    let client = unsafe { &*client.cast::<ReferenceClient>() };
    let map = unsafe { &*client.map };
    if key_len as usize > map.max_key_size || (key.is_null() && key_len != 0) {
        return STATUS_INVALID;
    }
    let key = if key_len == 0 {
        &[]
    } else {
        unsafe { std::slice::from_raw_parts(key, key_len as usize) }
    };
    let mut values = match map.values.lock() {
        Ok(values) => values,
        Err(_) => return STATUS_INTERNAL_ERROR,
    };
    let copied = match values.get(key) {
        Some(value) => {
            if value.len() > output_capacity as usize || (output.is_null() && !value.is_empty()) {
                return STATUS_INVALID;
            }
            value.clone()
        }
        None => return STATUS_MISSING,
    };
    if remove {
        values.remove(key);
    }
    if !copied.is_empty() {
        unsafe { std::ptr::copy_nonoverlapping(copied.as_ptr(), output, copied.len()) };
    }
    unsafe { *output_length = copied.len() as u64 };
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
