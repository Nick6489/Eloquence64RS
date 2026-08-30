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
    IniHasNoPaths,
    MissingDataFile(PathBuf),
    MissingPatch(PathBuf),
    InvalidPatch { path: PathBuf, reason: String },
}

impl fmt::Display for AssetError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MissingParentDirectory => {
                write!(formatter, "ECI DLL path has no parent directory")
            }
            Self::MissingIni(path) => write!(formatter, "ECI.INI is missing: {}", path.display()),
            Self::Io(error) => write!(formatter, "failed to prepare ECI assets: {error}"),
            Self::IniHasNoPaths => write!(formatter, "ECI.INI contains no voice data paths"),
            Self::MissingDataFile(path) => {
                write!(formatter, "ECI data file is missing: {}", path.display())
            }
            Self::MissingPatch(path) => write!(
                formatter,
                "native 16 kHz patch is missing: {}",
                path.display()
            ),
            Self::InvalidPatch { path, reason } => write!(
                formatter,
                "invalid native 16 kHz patch {}: {reason}",
                path.display()
            ),
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
    pub fn create(
        source_dll: &Path,
        data_directory: &Path,
        native_16khz: bool,
    ) -> Result<Self, AssetError> {
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

        let result = Self::populate(
            &directory,
            source_dll,
            &source_ini,
            data_directory,
            native_16khz,
        );
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
        native_16khz: bool,
    ) -> Result<(), AssetError> {
        fs::copy(source_dll, directory.join("ECI.DLL"))?;
        let ini = fs::read_to_string(source_ini)?;
        let filenames = referenced_filenames(&ini);
        if filenames.is_empty() {
            return Err(AssetError::IniHasNoPaths);
        }
        for filename in &filenames {
            let source = data_directory.join(filename);
            if !source.is_file() {
                return Err(AssetError::MissingDataFile(source));
            }
            let destination = directory.join(filename);
            fs::copy(&source, &destination)?;
            if native_16khz
                && destination
                    .extension()
                    .is_some_and(|ext| ext.eq_ignore_ascii_case("syn"))
            {
                let patch_name = Path::new(filename).with_extension("p16");
                let patch_path = data_directory.join(patch_name);
                if !patch_path.is_file() {
                    return Err(AssetError::MissingPatch(patch_path));
                }
                let original = fs::read(&destination)?;
                let patch = fs::read(&patch_path)?;
                let transformed = apply_p16(&original, &patch, &patch_path)?;
                fs::write(destination, transformed)?;
            }
        }
        let staged_directory = short_path(directory);
        let patched =
            reanchor_ini_paths(&ini, &staged_directory).ok_or(AssetError::IniHasNoPaths)?;
        fs::write(directory.join("ECI.INI"), patched)?;
        Ok(())
    }

    pub fn dll_path(&self) -> &Path {
        &self.dll_path
    }
}

fn referenced_filenames(ini: &str) -> Vec<String> {
    let mut filenames = Vec::new();
    for line in ini.lines() {
        let Some(equals) = line.find('=') else {
            continue;
        };
        let key = line[..equals].trim();
        if !key.eq_ignore_ascii_case("Path") && !key.eq_ignore_ascii_case("Path_Rom") {
            continue;
        }
        if let Some(filename) = line[equals + 1..]
            .trim()
            .rsplit(['\\', '/'])
            .next()
            .filter(|name| !name.is_empty())
        {
            if !filenames
                .iter()
                .any(|known: &String| known.eq_ignore_ascii_case(filename))
            {
                filenames.push(filename.to_owned());
            }
        }
    }
    filenames
}

fn apply_p16(original: &[u8], patch: &[u8], path: &Path) -> Result<Vec<u8>, AssetError> {
    let invalid = |reason: String| AssetError::InvalidPatch {
        path: path.to_owned(),
        reason,
    };
    if patch.get(..4) != Some(b"P16D") {
        return Err(invalid("bad magic".to_owned()));
    }
    let mut cursor = 4;
    let original_size = read_patch_u32(patch, &mut cursor)
        .ok_or_else(|| invalid("truncated header".to_owned()))? as usize;
    let new_size = read_patch_u32(patch, &mut cursor)
        .ok_or_else(|| invalid("truncated header".to_owned()))? as usize;
    let run_count = read_patch_u32(patch, &mut cursor)
        .ok_or_else(|| invalid("truncated header".to_owned()))? as usize;
    if original.len() != original_size {
        return Err(invalid(format!(
            "source size is {}, expected {original_size}",
            original.len()
        )));
    }
    let mut result = Vec::with_capacity(new_size);
    let mut source_cursor = 0usize;
    for _ in 0..run_count {
        let offset = read_patch_u32(patch, &mut cursor)
            .ok_or_else(|| invalid("truncated run".to_owned()))? as usize;
        let old_len = read_patch_u32(patch, &mut cursor)
            .ok_or_else(|| invalid("truncated run".to_owned()))? as usize;
        let new_len = read_patch_u32(patch, &mut cursor)
            .ok_or_else(|| invalid("truncated run".to_owned()))? as usize;
        if offset < source_cursor
            || offset
                .checked_add(old_len)
                .is_none_or(|end| end > original.len())
        {
            return Err(invalid("run is outside or overlaps the source".to_owned()));
        }
        let old_end = cursor
            .checked_add(old_len)
            .ok_or_else(|| invalid("run length overflow".to_owned()))?;
        let new_end = old_end
            .checked_add(new_len)
            .ok_or_else(|| invalid("run length overflow".to_owned()))?;
        if new_end > patch.len() {
            return Err(invalid("truncated run data".to_owned()));
        }
        if original[offset..offset + old_len] != patch[cursor..old_end] {
            return Err(invalid(format!(
                "source bytes do not match at offset {offset}"
            )));
        }
        result.extend_from_slice(&original[source_cursor..offset]);
        result.extend_from_slice(&patch[old_end..new_end]);
        source_cursor = offset + old_len;
        cursor = new_end;
    }
    if cursor != patch.len() {
        return Err(invalid("trailing patch data".to_owned()));
    }
    result.extend_from_slice(&original[source_cursor..]);
    if result.len() != new_size {
        return Err(invalid(format!(
            "result size is {}, expected {new_size}",
            result.len()
        )));
    }
    Ok(result)
}

fn read_patch_u32(bytes: &[u8], cursor: &mut usize) -> Option<u32> {
    let end = cursor.checked_add(4)?;
    let value = u32::from_le_bytes(bytes.get(*cursor..end)?.try_into().ok()?);
    *cursor = end;
    Some(value)
}

fn reanchor_ini_paths(ini: &str, data_directory: &Path) -> Option<String> {
    let mut path_count = 0;
    let mut patched = String::with_capacity(ini.len());
    for line in ini.split_inclusive('\n') {
        let (content, newline) = line
            .strip_suffix("\r\n")
            .map(|content| (content, "\r\n"))
            .or_else(|| line.strip_suffix('\n').map(|content| (content, "\n")))
            .unwrap_or((line, ""));
        let Some(equals) = content.find('=') else {
            patched.push_str(line);
            continue;
        };
        let key = content[..equals].trim();
        if !key.eq_ignore_ascii_case("Path") && !key.eq_ignore_ascii_case("Path_Rom") {
            patched.push_str(line);
            continue;
        }
        let old_path = content[equals + 1..].trim();
        let Some(filename) = old_path
            .rsplit(['\\', '/'])
            .next()
            .filter(|name| !name.is_empty())
        else {
            patched.push_str(line);
            continue;
        };
        patched.push_str(&content[..=equals]);
        patched.push_str(&data_directory.display().to_string());
        patched.push('\\');
        patched.push_str(filename);
        patched.push_str(newline);
        path_count += 1;
    }
    if path_count == 0 {
        None
    } else {
        Some(patched)
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
        fs::write(data.join("enu.syn"), b"voice data").unwrap();

        let prepared = PreparedEci::create(&source.join("ECI.DLL"), &data, false).unwrap();
        let patched = fs::read_to_string(prepared.dll_path().with_file_name("ECI.INI")).unwrap();
        assert!(!patched.contains(r"C:\dummy\"));
        let staged = prepared.dll_path().parent().unwrap();
        assert!(patched.contains(&short_path(staged).display().to_string()));
        assert_eq!(
            fs::read_to_string(source.join("ECI.INI")).unwrap(),
            original
        );

        drop(prepared);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn preparation_reanchors_existing_absolute_paths() {
        let root = std::env::temp_dir().join(format!(
            "eloquence-assets-reanchor-test-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&root);
        let source = root.join("source");
        let data = root.join("new-data");
        fs::create_dir_all(&source).unwrap();
        fs::create_dir_all(&data).unwrap();
        fs::write(source.join("ECI.DLL"), b"not a real dll").unwrap();
        fs::write(
            source.join("ECI.INI"),
            "[1.0]\r\nPath=C:\\old-install\\enu.syn\r\nPath_Rom=C:\\old-install\\jpnrom.dll\r\n",
        )
        .unwrap();
        fs::write(data.join("enu.syn"), b"voice data").unwrap();
        fs::write(data.join("jpnrom.dll"), b"rom data").unwrap();

        let prepared = PreparedEci::create(&source.join("ECI.DLL"), &data, false).unwrap();
        let patched = fs::read_to_string(prepared.dll_path().with_file_name("ECI.INI")).unwrap();
        let expected_root = short_path(prepared.dll_path().parent().unwrap())
            .display()
            .to_string();
        assert!(patched.contains(&format!("Path={expected_root}\\enu.syn")));
        assert!(patched.contains(&format!("Path_Rom={expected_root}\\jpnrom.dll")));

        drop(prepared);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn native_patch_is_validated_and_applied_only_to_staged_copy() {
        let original = b"abcdefgh";
        let mut patch = b"P16D".to_vec();
        patch.extend_from_slice(&(original.len() as u32).to_le_bytes());
        patch.extend_from_slice(&10_u32.to_le_bytes());
        patch.extend_from_slice(&1_u32.to_le_bytes());
        patch.extend_from_slice(&2_u32.to_le_bytes());
        patch.extend_from_slice(&2_u32.to_le_bytes());
        patch.extend_from_slice(&4_u32.to_le_bytes());
        patch.extend_from_slice(b"cd");
        patch.extend_from_slice(b"WXYZ");
        assert_eq!(
            apply_p16(original, &patch, Path::new("enu.p16")).unwrap(),
            b"abWXYZefgh"
        );
        let mut incompatible = original.to_vec();
        incompatible[2] = b'!';
        assert!(matches!(
            apply_p16(&incompatible, &patch, Path::new("enu.p16")),
            Err(AssetError::InvalidPatch { .. })
        ));
    }
}
