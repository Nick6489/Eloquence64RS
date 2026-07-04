use std::error::Error;
use std::ffi::{c_char, c_void, OsStr};
use std::fmt;
use std::os::windows::ffi::OsStrExt;

pub type EciHandle = *mut c_void;
pub type EciDictionaryHandle = *mut c_void;
pub type EciInputText = *const c_void;
pub type EciCallback =
    unsafe extern "system" fn(EciHandle, u32, i32, *mut c_void) -> EciCallbackReturn;
pub type OptionalEciCallback = Option<EciCallback>;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(i32)]
pub enum EciCallbackReturn {
    NotProcessed = 0,
    Processed = 1,
    Abort = 2,
}

type ModuleHandle = *mut c_void;

#[link(name = "kernel32")]
extern "system" {
    fn LoadLibraryW(path: *const u16) -> ModuleHandle;
    fn GetProcAddress(module: ModuleHandle, name: *const c_char) -> *mut c_void;
    fn FreeLibrary(module: ModuleHandle) -> i32;
    fn GetLastError() -> u32;
}

pub struct DynamicLibrary {
    handle: ModuleHandle,
}

impl DynamicLibrary {
    pub fn load(path: &OsStr) -> Result<Self, EciLoadError> {
        let path: Vec<u16> = path.encode_wide().chain(Some(0)).collect();
        let handle = unsafe { LoadLibraryW(path.as_ptr()) };
        if handle.is_null() {
            return Err(EciLoadError::LibraryLoadFailed(unsafe { GetLastError() }));
        }
        Ok(Self { handle })
    }

    unsafe fn symbol<T: Copy>(&self, name: &'static [u8]) -> Result<T, EciLoadError> {
        debug_assert_eq!(name.last(), Some(&0));
        let address = GetProcAddress(self.handle, name.as_ptr().cast());
        if address.is_null() {
            let printable_name = std::str::from_utf8_unchecked(&name[..name.len() - 1]);
            return Err(EciLoadError::SymbolNotFound {
                name: printable_name,
                windows_error: GetLastError(),
            });
        }
        // Windows function pointers and data pointers have the same size on
        // supported targets. Copying the bits avoids extending the lifetime of
        // a temporary Symbol wrapper; `EciApi` owns this library for longer
        // than every resolved function pointer.
        Ok(std::mem::transmute_copy(&address))
    }
}

impl Drop for DynamicLibrary {
    fn drop(&mut self) {
        if !self.handle.is_null() {
            unsafe {
                FreeLibrary(self.handle);
            }
            self.handle = std::ptr::null_mut();
        }
    }
}

#[derive(Debug, Eq, PartialEq)]
pub enum EciLoadError {
    LibraryLoadFailed(u32),
    SymbolNotFound {
        name: &'static str,
        windows_error: u32,
    },
}

impl fmt::Display for EciLoadError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::LibraryLoadFailed(error) => write!(formatter, "LoadLibraryW failed with {error}"),
            Self::SymbolNotFound {
                name,
                windows_error,
            } => write!(
                formatter,
                "ECI export {name} was not found (Windows error {windows_error})"
            ),
        }
    }
}

impl Error for EciLoadError {}

pub type EciNewEx = unsafe extern "system" fn(i32) -> EciHandle;
pub type EciDelete = unsafe extern "system" fn(EciHandle) -> EciHandle;
pub type EciRegisterCallback =
    unsafe extern "system" fn(EciHandle, OptionalEciCallback, *mut c_void);
pub type EciSetOutputBuffer = unsafe extern "system" fn(EciHandle, i32, *mut i16) -> i32;
pub type EciAddText = unsafe extern "system" fn(EciHandle, EciInputText) -> i32;
pub type EciInsertIndex = unsafe extern "system" fn(EciHandle, i32) -> i32;
pub type EciSynthesize = unsafe extern "system" fn(EciHandle) -> i32;
pub type EciSynchronize = unsafe extern "system" fn(EciHandle) -> i32;
pub type EciStop = unsafe extern "system" fn(EciHandle) -> i32;
pub type EciGetParam = unsafe extern "system" fn(EciHandle, i32) -> i32;
pub type EciSetParam = unsafe extern "system" fn(EciHandle, i32, i32) -> i32;
pub type EciGetVoiceParam = unsafe extern "system" fn(EciHandle, i32, i32) -> i32;
pub type EciSetVoiceParam = unsafe extern "system" fn(EciHandle, i32, i32, i32) -> i32;
pub type EciCopyVoice = unsafe extern "system" fn(EciHandle, i32, i32) -> i32;
pub type EciNewDictionary = unsafe extern "system" fn(EciHandle) -> EciDictionaryHandle;
pub type EciLoadDictionary =
    unsafe extern "system" fn(EciHandle, EciDictionaryHandle, i32, *const c_void) -> i32;
pub type EciSetDictionary = unsafe extern "system" fn(EciHandle, EciDictionaryHandle) -> i32;
pub type EciDeleteDictionary =
    unsafe extern "system" fn(EciHandle, EciDictionaryHandle) -> EciDictionaryHandle;

pub struct EciApi {
    _library: DynamicLibrary,
    pub new_ex: EciNewEx,
    pub delete: EciDelete,
    pub register_callback: EciRegisterCallback,
    pub set_output_buffer: EciSetOutputBuffer,
    pub add_text: EciAddText,
    pub insert_index: EciInsertIndex,
    pub synthesize: EciSynthesize,
    pub synchronize: EciSynchronize,
    pub stop: EciStop,
    pub get_param: EciGetParam,
    pub set_param: EciSetParam,
    pub get_voice_param: EciGetVoiceParam,
    pub set_voice_param: EciSetVoiceParam,
    pub copy_voice: EciCopyVoice,
    pub new_dictionary: EciNewDictionary,
    pub load_dictionary: EciLoadDictionary,
    pub set_dictionary: EciSetDictionary,
    pub delete_dictionary: EciDeleteDictionary,
}

impl EciApi {
    pub fn load(path: &OsStr) -> Result<Self, EciLoadError> {
        let library = DynamicLibrary::load(path)?;
        unsafe {
            Ok(Self {
                new_ex: library.symbol(b"eciNewEx\0")?,
                delete: library.symbol(b"eciDelete\0")?,
                register_callback: library.symbol(b"eciRegisterCallback\0")?,
                set_output_buffer: library.symbol(b"eciSetOutputBuffer\0")?,
                add_text: library.symbol(b"eciAddText\0")?,
                insert_index: library.symbol(b"eciInsertIndex\0")?,
                synthesize: library.symbol(b"eciSynthesize\0")?,
                synchronize: library.symbol(b"eciSynchronize\0")?,
                stop: library.symbol(b"eciStop\0")?,
                get_param: library.symbol(b"eciGetParam\0")?,
                set_param: library.symbol(b"eciSetParam\0")?,
                get_voice_param: library.symbol(b"eciGetVoiceParam\0")?,
                set_voice_param: library.symbol(b"eciSetVoiceParam\0")?,
                copy_voice: library.symbol(b"eciCopyVoice\0")?,
                new_dictionary: library.symbol(b"eciNewDict\0")?,
                load_dictionary: library.symbol(b"eciLoadDict\0")?,
                set_dictionary: library.symbol(b"eciSetDict\0")?,
                delete_dictionary: library.symbol(b"eciDeleteDict\0")?,
                _library: library,
            })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn owned_library_resolves_a_known_windows_export() {
        let library = DynamicLibrary::load(OsStr::new("kernel32.dll")).unwrap();
        let get_current_process_id: unsafe extern "system" fn() -> u32 =
            unsafe { library.symbol(b"GetCurrentProcessId\0").unwrap() };
        assert_ne!(unsafe { get_current_process_id() }, 0);
    }

    #[test]
    fn missing_export_returns_a_typed_error() {
        let library = DynamicLibrary::load(OsStr::new("kernel32.dll")).unwrap();
        let result = unsafe { library.symbol::<unsafe extern "system" fn()>(b"NotAnExport\0") };
        assert!(matches!(result, Err(EciLoadError::SymbolNotFound { .. })));
    }

    #[test]
    fn local_eci_dll_resolves_required_exports_when_available() {
        let Some(path) = std::env::var_os("ELOQUENCE_ECI_PATH") else {
            return;
        };
        EciApi::load(OsStr::new(&path)).unwrap();
    }

    #[cfg(target_pointer_width = "32")]
    #[test]
    fn engine_handles_are_32_bit_in_the_production_target() {
        assert_eq!(std::mem::size_of::<EciHandle>(), 4);
        assert_eq!(std::mem::size_of::<EciDictionaryHandle>(), 4);
    }
}
