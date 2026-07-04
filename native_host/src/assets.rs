//! Prepares an isolated ECI DLL/INI pair without rewriting add-on files.

use std::error::Error;
use std::ffi::OsString;
use std::fmt;
use std::fs;
use std::os::windows::ffi::{OsStrExt, OsStringExt};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

#[link(name = "kernel32")]
extern "system" {
    fn GetShortPathNameW(long_path: *const u16, short_path: *mut u16, buffer_length: u32) -> u32;
    fn WideCharToMultiByte(
        code_page: u32,
        flags: u32,
        wide: *const u16,
        wide_length: i32,
        bytes: *mut u8,
        byte_length: i32,
        default_character: *const u8,
        used_default_character: *mut i32,
    ) -> i32;
    fn GetLastError() -> u32;
}

#[derive(Debug)]
pub enum AssetError {
    MissingParentDirectory,
    MissingIni(PathBuf),
    Io(std::io::Error),
    IniHasNoPlaceholder,
}

impl fmt::Display for AssetError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MissingParentDirectory => {
                write!(formatter, "ECI DLL path has no parent directory")
            }
            Self::MissingIni(path) => write!(formatter, "ECI.INI is missing: {}", path.display()),
            Self::Io(error) => write!(formatter, "failed to prepare ECI assets: {error}"),
            Self::IniHasNoPlaceholder => write!(formatter, "ECI.INI contains no C:\\dummy\\ paths"),
        }
    }
}

impl Error for AssetError {}

impl From<std::io::Error> for AssetError {
    fn from(value: std::io::Error) -> Self {
        Self::Io(value)
    }
}

pub struct PreparedEci {
    directory: PathBuf,
    dll_path: PathBuf,
}

impl PreparedEci {
    pub fn create(source_dll: &Path, data_directory: &Path) -> Result<Self, AssetError> {
        let source_directory = source_dll
            .parent()
            .ok_or(AssetError::MissingParentDirectory)?;
        let source_ini = source_directory.join("ECI.INI");
        if !source_ini.is_file() {
            return Err(AssetError::MissingIni(source_ini));
        }

        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let directory = std::env::temp_dir().join(format!(
            "eloquence-native-host-{}-{nonce}",
            std::process::id()
        ));
        fs::create_dir(&directory)?;

        let result = Self::populate(&directory, source_dll, &source_ini, data_directory);
        if let Err(error) = result {
            let _ = fs::remove_dir_all(&directory);
            return Err(error);
        }
        Ok(Self {
            dll_path: directory.join("ECI.DLL"),
            directory,
        })
    }

    fn populate(
        directory: &Path,
        source_dll: &Path,
        source_ini: &Path,
        data_directory: &Path,
    ) -> Result<(), AssetError> {
        fs::copy(source_dll, directory.join("ECI.DLL"))?;
        let ini = fs::read_to_string(source_ini)?;
        let data_directory = short_path(data_directory);
        let replacement = format!("{}\\", data_directory.display());
        let patched = ini.replace("C:\\dummy\\", &replacement);
        if patched == ini {
            return Err(AssetError::IniHasNoPlaceholder);
        }
        fs::write(directory.join("ECI.INI"), patched)?;
        Ok(())
    }

    pub fn dll_path(&self) -> &Path {
        &self.dll_path
    }
}

impl Drop for PreparedEci {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.directory);
    }
}

pub fn short_path(path: &Path) -> PathBuf {
    let wide: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
    let required = unsafe { GetShortPathNameW(wide.as_ptr(), std::ptr::null_mut(), 0) };
    if required == 0 {
        return path.to_owned();
    }
    let mut buffer = vec![0_u16; required as usize];
    let written = unsafe { GetShortPathNameW(wide.as_ptr(), buffer.as_mut_ptr(), required) };
    if written == 0 || written >= required {
        return path.to_owned();
    }
    buffer.truncate(written as usize);
    PathBuf::from(OsString::from_wide(&buffer))
}

pub fn system_ansi_path(path: &Path) -> Result<Vec<u8>, u32> {
    let path = short_path(path);
    let wide: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
    let required = unsafe {
        WideCharToMultiByte(
            0,
            0,
            wide.as_ptr(),
            -1,
            std::ptr::null_mut(),
            0,
            std::ptr::null(),
            std::ptr::null_mut(),
        )
    };
    if required == 0 {
        return Err(unsafe { GetLastError() });
    }
    let mut bytes = vec![0_u8; required as usize];
    let written = unsafe {
        WideCharToMultiByte(
            0,
            0,
            wide.as_ptr(),
            -1,
            bytes.as_mut_ptr(),
            required,
            std::ptr::null(),
            std::ptr::null_mut(),
        )
    };
    if written == 0 {
        return Err(unsafe { GetLastError() });
    }
    bytes.truncate(written as usize);
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preparation_patches_a_copy_and_preserves_the_source_ini() {
        let root =
            std::env::temp_dir().join(format!("eloquence-assets-test-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let source = root.join("source");
        let data = root.join("data");
        fs::create_dir_all(&source).unwrap();
        fs::create_dir_all(&data).unwrap();
        fs::write(source.join("ECI.DLL"), b"not a real dll").unwrap();
        let original = "[1.0]\nPath=C:\\dummy\\enu.syn\n";
        fs::write(source.join("ECI.INI"), original).unwrap();

        let prepared = PreparedEci::create(&source.join("ECI.DLL"), &data).unwrap();
        let patched = fs::read_to_string(prepared.dll_path().with_file_name("ECI.INI")).unwrap();
        assert!(!patched.contains(r"C:\dummy\"));
        assert!(patched.contains(&short_path(&data).display().to_string()));
        assert_eq!(
            fs::read_to_string(source.join("ECI.INI")).unwrap(),
            original
        );

        drop(prepared);
        let _ = fs::remove_dir_all(root);
    }
}
