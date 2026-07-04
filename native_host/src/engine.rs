//! Safe ownership and callback handling around the dynamically loaded ECI API.

use crate::assets::system_ansi_path;
use crate::dictionaries;
use crate::eci::{EciApi, EciCallbackReturn, EciDictionaryHandle, EciHandle, EciStop};
use crate::progress::{ProgressEvent, ProgressTracker, FINAL_INDEX};
use std::collections::HashMap;
use std::error::Error;
use std::ffi::c_void;
use std::fmt;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::SyncSender;
use std::sync::{Arc, Mutex};

const WAVEFORM_BUFFER_MESSAGE: u32 = 0;
const INDEX_REPLY_MESSAGE: u32 = 2;
pub const DEFAULT_BUFFER_SAMPLES: usize = 3_300;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EngineEvent {
    Audio {
        generation: u64,
        samples: Vec<i16>,
    },
    Index {
        generation: u64,
        value: u32,
        recovered: bool,
    },
    Done {
        generation: u64,
    },
    Stopped {
        generation: u64,
    },
    CallbackError(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EngineError {
    CreateHandleFailed,
    SetOutputBufferFailed,
    NoActiveGeneration,
    TextContainsNul,
    AddTextFailed,
    InsertIndexFailed(u32),
    SynthesizeFailed,
    SynchronizeFailed,
    StopFailed,
    EventQueueUnavailable,
    CreateDictionaryFailed,
    DictionaryPathEncodingFailed { path: String, windows_error: u32 },
    LoadDictionaryFailed { path: String, error: i32 },
    SetDictionaryFailed(i32),
}

impl fmt::Display for EngineError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::CreateHandleFailed => write!(formatter, "eciNewEx failed"),
            Self::SetOutputBufferFailed => write!(formatter, "eciSetOutputBuffer failed"),
            Self::NoActiveGeneration => write!(formatter, "no synthesis generation is active"),
            Self::TextContainsNul => write!(formatter, "ECI input text contains a NUL byte"),
            Self::AddTextFailed => write!(formatter, "eciAddText failed"),
            Self::InsertIndexFailed(index) => write!(formatter, "eciInsertIndex({index}) failed"),
            Self::SynthesizeFailed => write!(formatter, "eciSynthesize failed"),
            Self::SynchronizeFailed => write!(formatter, "eciSynchronize failed"),
            Self::StopFailed => write!(formatter, "eciStop failed"),
            Self::EventQueueUnavailable => {
                write!(formatter, "engine event queue is full or closed")
            }
            Self::CreateDictionaryFailed => write!(formatter, "eciNewDict failed"),
            Self::DictionaryPathEncodingFailed {
                path,
                windows_error,
            } => write!(
                formatter,
                "dictionary path cannot be encoded for ECI ({windows_error}): {path}"
            ),
            Self::LoadDictionaryFailed { path, error } => {
                write!(formatter, "eciLoadDict failed with {error}: {path}")
            }
            Self::SetDictionaryFailed(error) => {
                write!(formatter, "eciSetDict failed with {error}")
            }
        }
    }
}

impl Error for EngineError {}

struct CallbackContext {
    output_buffer: Box<[i16]>,
    progress: Mutex<ProgressTracker>,
    events: SyncSender<EngineEvent>,
    cancellation_requested: Arc<AtomicBool>,
}

impl CallbackContext {
    fn send_progress(&self, event: ProgressEvent) -> bool {
        let event = match event {
            ProgressEvent::Index {
                generation,
                value,
                recovered,
            } => EngineEvent::Index {
                generation,
                value,
                recovered,
            },
            ProgressEvent::Done { generation } => EngineEvent::Done { generation },
            ProgressEvent::Stopped { generation } => EngineEvent::Stopped { generation },
        };
        self.events.try_send(event).is_ok()
    }

    fn complete(&self) -> bool {
        let events = self
            .progress
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .complete();
        events.into_iter().all(|event| self.send_progress(event))
    }
}

unsafe extern "system" fn eci_callback(
    _handle: EciHandle,
    message: u32,
    parameter: i32,
    user_data: *mut c_void,
) -> EciCallbackReturn {
    let result = catch_unwind(AssertUnwindSafe(|| {
        if user_data.is_null() {
            return EciCallbackReturn::Abort;
        }
        let context = &*(user_data.cast::<CallbackContext>());
        if context.cancellation_requested.load(Ordering::Acquire) {
            return EciCallbackReturn::Abort;
        }
        match message {
            WAVEFORM_BUFFER_MESSAGE => handle_audio_callback(context, parameter),
            INDEX_REPLY_MESSAGE => handle_index_callback(context, parameter),
            _ => EciCallbackReturn::Processed,
        }
    }));
    result.unwrap_or(EciCallbackReturn::Abort)
}

struct StopState {
    /// Stored as an integer so the synchronized state has an unambiguous
    /// Send/Sync representation even though ECI's public handle is a pointer.
    handle: Mutex<usize>,
    cancellation_requested: Arc<AtomicBool>,
}

#[derive(Clone)]
pub struct StopController {
    state: Arc<StopState>,
    stop_function: EciStop,
}

impl StopController {
    /// Requests cancellation from a control thread while `eciSynchronize` is
    /// blocked on the synthesis thread. Returns false if teardown has started.
    pub fn request_stop(&self) -> bool {
        self.state
            .cancellation_requested
            .store(true, Ordering::Release);
        let handle = *self
            .state
            .handle
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if handle == 0 {
            return false;
        }
        unsafe {
            (self.stop_function)(handle as EciHandle);
        }
        true
    }
}

fn handle_audio_callback(context: &CallbackContext, sample_count: i32) -> EciCallbackReturn {
    let Ok(sample_count) = usize::try_from(sample_count) else {
        let _ = context.events.try_send(EngineEvent::CallbackError(
            "ECI returned a negative sample count".to_owned(),
        ));
        return EciCallbackReturn::Abort;
    };
    if sample_count > context.output_buffer.len() {
        let _ = context.events.try_send(EngineEvent::CallbackError(format!(
            "ECI returned {sample_count} samples for a {}-sample buffer",
            context.output_buffer.len()
        )));
        return EciCallbackReturn::Abort;
    }

    let generation = context
        .progress
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .current_generation();
    if let Some(generation) = generation {
        let samples = context.output_buffer[..sample_count].to_vec();
        if context
            .events
            .try_send(EngineEvent::Audio {
                generation,
                samples,
            })
            .is_err()
        {
            return EciCallbackReturn::Abort;
        }
    }
    EciCallbackReturn::Processed
}

fn handle_index_callback(context: &CallbackContext, index: i32) -> EciCallbackReturn {
    let index = index as u32;
    if index == FINAL_INDEX {
        if !context.complete() {
            return EciCallbackReturn::Abort;
        }
    } else {
        let event = context
            .progress
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .engine_index(index);
        if let Some(event) = event {
            if !context.send_progress(event) {
                return EciCallbackReturn::Abort;
            }
        }
    }
    EciCallbackReturn::Processed
}

/// Owns one ECI instance. All methods except a future stop handle are intended
/// to be called from the dedicated synthesis thread.
pub struct EciEngine {
    api: EciApi,
    handle: EciHandle,
    callback_context: Box<CallbackContext>,
    stop_state: Arc<StopState>,
    dictionary_handles: HashMap<String, EciDictionaryHandle>,
}

impl EciEngine {
    pub fn new(
        api: EciApi,
        language_id: i32,
        events: SyncSender<EngineEvent>,
    ) -> Result<Self, EngineError> {
        let handle = unsafe { (api.new_ex)(language_id) };
        if handle.is_null() {
            return Err(EngineError::CreateHandleFailed);
        }

        let mut callback_context = Box::new(CallbackContext {
            output_buffer: vec![0_i16; DEFAULT_BUFFER_SAMPLES].into_boxed_slice(),
            progress: Mutex::new(ProgressTracker::default()),
            events,
            cancellation_requested: Arc::new(AtomicBool::new(false)),
        });
        let stop_state = Arc::new(StopState {
            handle: Mutex::new(handle as usize),
            cancellation_requested: Arc::clone(&callback_context.cancellation_requested),
        });
        let context_pointer = (&mut *callback_context as *mut CallbackContext).cast::<c_void>();
        unsafe {
            (api.register_callback)(handle, Some(eci_callback), context_pointer);
        }
        let output_result = unsafe {
            (api.set_output_buffer)(
                handle,
                callback_context.output_buffer.len() as i32,
                callback_context.output_buffer.as_mut_ptr(),
            )
        };
        if output_result == 0 {
            unsafe {
                (api.register_callback)(handle, None, std::ptr::null_mut());
                (api.delete)(handle);
            }
            return Err(EngineError::SetOutputBufferFailed);
        }

        Ok(Self {
            api,
            handle,
            callback_context,
            stop_state,
            dictionary_handles: HashMap::new(),
        })
    }

    pub fn begin_generation(&self, generation: u64) {
        self.stop_state
            .cancellation_requested
            .store(false, Ordering::Release);
        self.callback_context
            .progress
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .begin(generation);
    }

    pub fn add_text(&self, text: &[u8]) -> Result<(), EngineError> {
        self.require_active_generation()?;
        if text.contains(&0) {
            return Err(EngineError::TextContainsNul);
        }
        let mut terminated = Vec::with_capacity(text.len() + 1);
        terminated.extend_from_slice(text);
        terminated.push(0);
        let result = unsafe { (self.api.add_text)(self.handle, terminated.as_ptr().cast()) };
        if result == 0 {
            return Err(EngineError::AddTextFailed);
        }
        Ok(())
    }

    pub fn insert_index(&self, index: u32) -> Result<(), EngineError> {
        self.require_active_generation()?;
        let result = unsafe { (self.api.insert_index)(self.handle, index as i32) };
        if result == 0 {
            return Err(EngineError::InsertIndexFailed(index));
        }
        self.callback_context
            .progress
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .insert_index(index);
        Ok(())
    }

    pub fn synthesize(&self) -> Result<(), EngineError> {
        self.require_active_generation()?;
        let synthesized = unsafe { (self.api.synthesize)(self.handle) } != 0;
        let synchronized = synthesized && unsafe { (self.api.synchronize)(self.handle) } != 0;
        if self
            .stop_state
            .cancellation_requested
            .load(Ordering::Acquire)
        {
            let event = self
                .callback_context
                .progress
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .stop();
            if let Some(event) = event {
                if !self.callback_context.send_progress(event) {
                    return Err(EngineError::EventQueueUnavailable);
                }
            }
            return Ok(());
        }
        if !self.callback_context.complete() {
            return Err(EngineError::EventQueueUnavailable);
        }
        if !synthesized {
            return Err(EngineError::SynthesizeFailed);
        }
        if !synchronized {
            return Err(EngineError::SynchronizeFailed);
        }
        Ok(())
    }

    pub fn stop(&self) -> Result<(), EngineError> {
        if !self.stop_controller().request_stop() {
            return Err(EngineError::StopFailed);
        }
        Ok(())
    }

    pub fn stop_controller(&self) -> StopController {
        StopController {
            state: Arc::clone(&self.stop_state),
            stop_function: self.api.stop,
        }
    }

    pub fn set_param(&self, parameter: i32, value: i32) -> i32 {
        unsafe { (self.api.set_param)(self.handle, parameter, value) }
    }

    pub fn get_param(&self, parameter: i32) -> i32 {
        unsafe { (self.api.get_param)(self.handle, parameter) }
    }

    pub fn set_voice_param(&self, parameter: i32, value: i32) -> i32 {
        unsafe { (self.api.set_voice_param)(self.handle, 0, parameter, value) }
    }

    pub fn get_voice_param(&self, parameter: i32) -> i32 {
        unsafe { (self.api.get_voice_param)(self.handle, 0, parameter) }
    }

    pub fn copy_voice(&self, variant: i32) -> i32 {
        unsafe { (self.api.copy_voice)(self.handle, variant, 0) }
    }

    pub fn load_dictionaries(
        &mut self,
        language_code: &str,
        data_directory: &Path,
    ) -> Result<(), EngineError> {
        let language_code = language_code.to_ascii_lowercase();
        if let Some(&dictionary) = self.dictionary_handles.get(&language_code) {
            return self.set_dictionary(dictionary);
        }

        let dictionary = unsafe { (self.api.new_dictionary)(self.handle) };
        if dictionary.is_null() {
            return Err(EngineError::CreateDictionaryFailed);
        }
        let result = self.populate_dictionary(dictionary, &language_code, data_directory);
        if let Err(error) = result {
            unsafe {
                (self.api.delete_dictionary)(self.handle, dictionary);
            }
            return Err(error);
        }
        self.set_dictionary(dictionary)?;
        self.dictionary_handles.insert(language_code, dictionary);
        Ok(())
    }

    fn populate_dictionary(
        &self,
        dictionary: EciDictionaryHandle,
        language_code: &str,
        data_directory: &Path,
    ) -> Result<(), EngineError> {
        for (volume, candidates) in dictionaries::candidates(language_code).iter().enumerate() {
            let Some(path) = candidates
                .iter()
                .map(|name| data_directory.join(name))
                .find(|path| path.is_file())
            else {
                continue;
            };
            let encoded = system_ansi_path(&path).map_err(|windows_error| {
                EngineError::DictionaryPathEncodingFailed {
                    path: path.display().to_string(),
                    windows_error,
                }
            })?;
            let result = unsafe {
                (self.api.load_dictionary)(
                    self.handle,
                    dictionary,
                    volume as i32,
                    encoded.as_ptr().cast(),
                )
            };
            if result != 0 {
                return Err(EngineError::LoadDictionaryFailed {
                    path: path.display().to_string(),
                    error: result,
                });
            }
        }
        Ok(())
    }

    fn set_dictionary(&self, dictionary: EciDictionaryHandle) -> Result<(), EngineError> {
        let result = unsafe { (self.api.set_dictionary)(self.handle, dictionary) };
        if result != 0 {
            return Err(EngineError::SetDictionaryFailed(result));
        }
        Ok(())
    }

    fn require_active_generation(&self) -> Result<u64, EngineError> {
        self.callback_context
            .progress
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .current_generation()
            .ok_or(EngineError::NoActiveGeneration)
    }
}

impl Drop for EciEngine {
    fn drop(&mut self) {
        let mut published_handle = self
            .stop_state
            .handle
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        *published_handle = 0;
        unsafe {
            (self.api.register_callback)(self.handle, None, std::ptr::null_mut());
            for (_, dictionary) in self.dictionary_handles.drain() {
                (self.api.delete_dictionary)(self.handle, dictionary);
            }
            (self.api.delete)(self.handle);
        }
        self.handle = std::ptr::null_mut();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(target_pointer_width = "32")]
    use crate::eci::EciApi;
    #[cfg(target_pointer_width = "32")]
    use std::ffi::OsStr;
    #[cfg(target_pointer_width = "32")]
    use std::fs;
    #[cfg(target_pointer_width = "32")]
    use std::path::{Path, PathBuf};
    use std::sync::mpsc;

    fn context() -> (Box<CallbackContext>, mpsc::Receiver<EngineEvent>) {
        let (sender, receiver) = mpsc::sync_channel(64);
        let context = Box::new(CallbackContext {
            output_buffer: vec![1, -2, 3, -4].into_boxed_slice(),
            progress: Mutex::new(ProgressTracker::default()),
            events: sender,
            cancellation_requested: Arc::new(AtomicBool::new(false)),
        });
        (context, receiver)
    }

    #[test]
    fn audio_callback_copies_samples_before_returning_buffer_to_eci() {
        let (context, receiver) = context();
        context.progress.lock().unwrap().begin(8);
        assert_eq!(
            handle_audio_callback(&context, 3),
            EciCallbackReturn::Processed
        );
        assert_eq!(
            receiver.recv().unwrap(),
            EngineEvent::Audio {
                generation: 8,
                samples: vec![1, -2, 3],
            }
        );
    }

    #[test]
    fn invalid_audio_length_aborts_without_reading_outside_the_buffer() {
        let (context, receiver) = context();
        context.progress.lock().unwrap().begin(8);
        assert_eq!(handle_audio_callback(&context, 5), EciCallbackReturn::Abort);
        assert!(matches!(
            receiver.recv().unwrap(),
            EngineEvent::CallbackError(_)
        ));
    }

    #[test]
    fn final_callback_recovers_pending_index_then_reports_done_once() {
        let (context, receiver) = context();
        {
            let mut progress = context.progress.lock().unwrap();
            progress.begin(111);
            progress.insert_index(5544);
        }
        assert_eq!(
            handle_index_callback(&context, FINAL_INDEX as i32),
            EciCallbackReturn::Processed
        );
        assert!(context.complete());

        assert_eq!(
            receiver.try_iter().collect::<Vec<_>>(),
            vec![
                EngineEvent::Index {
                    generation: 111,
                    value: 5544,
                    recovered: true,
                },
                EngineEvent::Done { generation: 111 },
            ]
        );
    }

    #[cfg(target_pointer_width = "32")]
    fn prepare_test_eci(source_dll: &Path) -> PathBuf {
        let source_directory = source_dll.parent().unwrap();
        let test_directory =
            std::env::temp_dir().join(format!("eloquence-native-host-{}", std::process::id()));
        let _ = fs::remove_dir_all(&test_directory);
        fs::create_dir_all(&test_directory).unwrap();

        let test_dll = test_directory.join("ECI.DLL");
        fs::copy(source_dll, &test_dll).unwrap();
        let ini = fs::read_to_string(source_directory.join("ECI.INI")).unwrap();
        let data_directory = source_directory.canonicalize().unwrap();
        let replacement = format!("{}\\", data_directory.display());
        let patched_ini = ini.replace("C:\\dummy\\", &replacement);
        assert_ne!(ini, patched_ini, "test ECI.INI contained no dummy paths");
        fs::write(test_directory.join("ECI.INI"), patched_ini).unwrap();
        test_dll
    }

    /// This test is opt-in because it uses the proprietary local engine data.
    /// It runs in the actual 32-bit target and never rewrites the add-on copy.
    #[cfg(target_pointer_width = "32")]
    #[test]
    fn local_eci_synthesizes_pcm_and_ordered_progress_when_available() {
        let Some(source_dll) = std::env::var_os("ELOQUENCE_ECI_PATH") else {
            return;
        };
        let test_dll = prepare_test_eci(Path::new(&source_dll));
        let api = EciApi::load(OsStr::new(&test_dll)).unwrap();
        let (sender, receiver) = mpsc::sync_channel(64);
        {
            let engine = EciEngine::new(api, 65_536, sender).unwrap();
            engine.set_param(1, 1); // annotated input
            engine.begin_generation(77);
            engine.add_text(b"Native Eloquence host test.").unwrap();
            engine.insert_index(42).unwrap();
            engine.insert_index(FINAL_INDEX).unwrap();
            engine.synthesize().unwrap();
        }

        let events: Vec<_> = receiver.try_iter().collect();
        assert!(events.iter().any(
            |event| matches!(event, EngineEvent::Audio { samples, .. } if !samples.is_empty())
        ));
        let index_position = events
            .iter()
            .position(|event| matches!(event, EngineEvent::Index { value: 42, .. }))
            .expect("index 42 was not reported");
        let done_position = events
            .iter()
            .position(|event| matches!(event, EngineEvent::Done { generation: 77 }))
            .expect("completion was not reported");
        assert!(index_position < done_position);

        let test_directory = test_dll.parent().unwrap();
        let _ = fs::remove_dir_all(test_directory);
    }
}
