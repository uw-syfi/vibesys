use std::ffi::{c_char, c_int, c_void, CStr, CString};
use std::path::Path;
use std::sync::{Arc, Mutex};

pub const ABI_VERSION: u32 = 1;
pub const STATUS_OK: u32 = 0;
pub const STATUS_FULL: u32 = 1;
pub const STATUS_EMPTY: u32 = 2;
pub const STATUS_INVALID: u32 = 3;
pub const STATUS_INTERNAL_ERROR: u32 = 4;

pub type StackHandle = *mut c_void;
pub type ProducerHandle = *mut c_void;
pub type ConsumerHandle = *mut c_void;

type AbiVersionFn = unsafe extern "C" fn() -> u32;
type StackCreateFn = unsafe extern "C" fn(u64, u64, u32, u32, *mut StackHandle) -> u32;
type StackDestroyFn = unsafe extern "C" fn(StackHandle);
type ProducerCreateFn = unsafe extern "C" fn(StackHandle, u32, *mut ProducerHandle) -> u32;
type ProducerDestroyFn = unsafe extern "C" fn(ProducerHandle);
type ConsumerCreateFn = unsafe extern "C" fn(StackHandle, u32, *mut ConsumerHandle) -> u32;
type ConsumerDestroyFn = unsafe extern "C" fn(ConsumerHandle);
type PushFn = unsafe extern "C" fn(ProducerHandle, *const u8, u64) -> u32;
type PopFn = unsafe extern "C" fn(ConsumerHandle, *mut u8, u64, *mut u64) -> u32;

#[derive(Clone)]
pub struct Api {
    _library: Option<Arc<DynamicLibrary>>,
    stack_create: StackCreateFn,
    stack_destroy: StackDestroyFn,
    producer_create: ProducerCreateFn,
    producer_destroy: ProducerDestroyFn,
    consumer_create: ConsumerCreateFn,
    consumer_destroy: ConsumerDestroyFn,
    push: PushFn,
    pop: PopFn,
}

pub struct Stack {
    api: Api,
    handle: StackHandle,
}

unsafe impl Send for Stack {}
unsafe impl Sync for Stack {}

pub struct Producer {
    api: Api,
    handle: ProducerHandle,
}

unsafe impl Send for Producer {}

pub struct Consumer {
    api: Api,
    handle: ConsumerHandle,
}

unsafe impl Send for Consumer {}

impl Api {
    pub fn load(path: &Path) -> Result<Self, String> {
        let library = Arc::new(DynamicLibrary::open(path)?);
        unsafe {
            let abi_version: AbiVersionFn = library.symbol(b"vsst_abi_version\0")?;
            let version = abi_version();
            if version != ABI_VERSION {
                return Err(format!(
                    "candidate ABI version {version}, expected {ABI_VERSION}"
                ));
            }
            Ok(Self {
                _library: Some(library.clone()),
                stack_create: library.symbol(b"vsst_stack_create\0")?,
                stack_destroy: library.symbol(b"vsst_stack_destroy\0")?,
                producer_create: library.symbol(b"vsst_producer_create\0")?,
                producer_destroy: library.symbol(b"vsst_producer_destroy\0")?,
                consumer_create: library.symbol(b"vsst_consumer_create\0")?,
                consumer_destroy: library.symbol(b"vsst_consumer_destroy\0")?,
                push: library.symbol(b"vsst_try_push\0")?,
                pop: library.symbol(b"vsst_try_pop\0")?,
            })
        }
    }

    pub fn reference() -> Self {
        Self {
            _library: None,
            stack_create: reference_stack_create,
            stack_destroy: reference_stack_destroy,
            producer_create: reference_producer_create,
            producer_destroy: reference_producer_destroy,
            consumer_create: reference_consumer_create,
            consumer_destroy: reference_consumer_destroy,
            push: reference_push,
            pop: reference_pop,
        }
    }

    pub fn create_stack(
        &self,
        capacity: u64,
        max_value_size: u64,
        producer_count: u32,
        consumer_count: u32,
    ) -> Result<Stack, String> {
        let mut handle = std::ptr::null_mut();
        let status = unsafe {
            (self.stack_create)(
                capacity,
                max_value_size,
                producer_count,
                consumer_count,
                &mut handle,
            )
        };
        if status != STATUS_OK || handle.is_null() {
            return Err(format!("vsst_stack_create returned status {status}"));
        }
        Ok(Stack {
            api: self.clone(),
            handle,
        })
    }
}

impl Stack {
    pub fn create_producer(&self, id: u32) -> Result<Producer, String> {
        let mut handle = std::ptr::null_mut();
        let status = unsafe { (self.api.producer_create)(self.handle, id, &mut handle) };
        if status != STATUS_OK || handle.is_null() {
            return Err(format!(
                "vsst_producer_create({id}) returned status {status}"
            ));
        }
        Ok(Producer {
            api: self.api.clone(),
            handle,
        })
    }

    pub fn create_consumer(&self, id: u32) -> Result<Consumer, String> {
        let mut handle = std::ptr::null_mut();
        let status = unsafe { (self.api.consumer_create)(self.handle, id, &mut handle) };
        if status != STATUS_OK || handle.is_null() {
            return Err(format!(
                "vsst_consumer_create({id}) returned status {status}"
            ));
        }
        Ok(Consumer {
            api: self.api.clone(),
            handle,
        })
    }
}

impl Drop for Stack {
    fn drop(&mut self) {
        unsafe { (self.api.stack_destroy)(self.handle) };
    }
}

impl Producer {
    pub fn push(&mut self, value: &[u8]) -> u32 {
        unsafe { (self.api.push)(self.handle, value.as_ptr(), value.len() as u64) }
    }
}

impl Drop for Producer {
    fn drop(&mut self) {
        unsafe { (self.api.producer_destroy)(self.handle) };
    }
}

impl Consumer {
    pub fn pop_raw(
        &mut self,
        output: *mut u8,
        output_capacity: u64,
        output_length: &mut u64,
    ) -> u32 {
        unsafe {
            (self.api.pop)(
                self.handle,
                output,
                output_capacity,
                output_length as *mut u64,
            )
        }
    }

    pub fn pop(&mut self, output: &mut [u8]) -> Result<(u32, usize), String> {
        let mut length = u64::MAX;
        let status = self.pop_raw(output.as_mut_ptr(), output.len() as u64, &mut length);
        if status == STATUS_EMPTY {
            if length != u64::MAX {
                return Err("candidate modified output length for an empty pop".to_string());
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
}

impl Drop for Consumer {
    fn drop(&mut self) {
        unsafe { (self.api.consumer_destroy)(self.handle) };
    }
}

struct ReferenceStack {
    capacity: usize,
    max_value_size: usize,
    values: Mutex<Vec<Vec<u8>>>,
}

struct ReferenceProducer {
    stack: *mut ReferenceStack,
}

struct ReferenceConsumer {
    stack: *mut ReferenceStack,
}

unsafe extern "C" fn reference_stack_create(
    capacity: u64,
    max_value_size: u64,
    _producer_count: u32,
    _consumer_count: u32,
    output: *mut StackHandle,
) -> u32 {
    if capacity == 0 || max_value_size == 0 || output.is_null() {
        return STATUS_INVALID;
    }
    let stack = Box::new(ReferenceStack {
        capacity: capacity as usize,
        max_value_size: max_value_size as usize,
        values: Mutex::new(Vec::new()),
    });
    unsafe { *output = Box::into_raw(stack).cast() };
    STATUS_OK
}

unsafe extern "C" fn reference_stack_destroy(stack: StackHandle) {
    if !stack.is_null() {
        drop(unsafe { Box::from_raw(stack.cast::<ReferenceStack>()) });
    }
}

unsafe extern "C" fn reference_producer_create(
    stack: StackHandle,
    _id: u32,
    output: *mut ProducerHandle,
) -> u32 {
    if stack.is_null() || output.is_null() {
        return STATUS_INVALID;
    }
    let producer = Box::new(ReferenceProducer {
        stack: stack.cast(),
    });
    unsafe { *output = Box::into_raw(producer).cast() };
    STATUS_OK
}

unsafe extern "C" fn reference_producer_destroy(producer: ProducerHandle) {
    if !producer.is_null() {
        drop(unsafe { Box::from_raw(producer.cast::<ReferenceProducer>()) });
    }
}

unsafe extern "C" fn reference_consumer_create(
    stack: StackHandle,
    _id: u32,
    output: *mut ConsumerHandle,
) -> u32 {
    if stack.is_null() || output.is_null() {
        return STATUS_INVALID;
    }
    let consumer = Box::new(ReferenceConsumer {
        stack: stack.cast(),
    });
    unsafe { *output = Box::into_raw(consumer).cast() };
    STATUS_OK
}

unsafe extern "C" fn reference_consumer_destroy(consumer: ConsumerHandle) {
    if !consumer.is_null() {
        drop(unsafe { Box::from_raw(consumer.cast::<ReferenceConsumer>()) });
    }
}

unsafe extern "C" fn reference_push(producer: ProducerHandle, data: *const u8, length: u64) -> u32 {
    if producer.is_null() || (data.is_null() && length != 0) {
        return STATUS_INVALID;
    }
    let producer = unsafe { &*producer.cast::<ReferenceProducer>() };
    let stack = unsafe { &*producer.stack };
    if length as usize > stack.max_value_size {
        return STATUS_INVALID;
    }
    let mut values = match stack.values.lock() {
        Ok(values) => values,
        Err(_) => return STATUS_INTERNAL_ERROR,
    };
    if values.len() == stack.capacity {
        return STATUS_FULL;
    }
    let value = if length == 0 {
        Vec::new()
    } else {
        unsafe { std::slice::from_raw_parts(data, length as usize) }.to_vec()
    };
    values.push(value);
    STATUS_OK
}

unsafe extern "C" fn reference_pop(
    consumer: ConsumerHandle,
    output: *mut u8,
    output_capacity: u64,
    output_length: *mut u64,
) -> u32 {
    if consumer.is_null() || output_length.is_null() {
        return STATUS_INVALID;
    }
    let consumer = unsafe { &*consumer.cast::<ReferenceConsumer>() };
    let stack = unsafe { &*consumer.stack };
    let mut values = match stack.values.lock() {
        Ok(values) => values,
        Err(_) => return STATUS_INTERNAL_ERROR,
    };
    let Some(value) = values.last() else {
        return STATUS_EMPTY;
    };
    if value.len() > output_capacity as usize || (output.is_null() && !value.is_empty()) {
        return STATUS_INVALID;
    }
    let value = values.pop().expect("stack top vanished after last()");
    if !value.is_empty() {
        unsafe { std::ptr::copy_nonoverlapping(value.as_ptr(), output, value.len()) };
    }
    unsafe { *output_length = value.len() as u64 };
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
