//! Single-client host server over inherited stdin/stdout pipes.

use crate::assets::PreparedEci;
use crate::eci::EciApi;
use crate::engine::{EciEngine, EngineEvent, StopController};
use crate::protocol::{Frame, MessageKind, ProtocolError, AUTH_KEY_LEN};
use crate::wire::{self, ClientCommand, InitializeConfig};
use std::error::Error;
use std::fmt;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::sync::{Arc, Mutex};
use std::thread;

const QUEUE_CAPACITY: usize = 64;
const ECI_INPUT_TYPE: i32 = 1;
const ECI_LANGUAGE_DIALECT: i32 = 9;
const ECI_ABBREVIATION_DICTIONARY: i32 = 41;
const ECI_PHRASE_PREDICTION: i32 = 42;

#[derive(Debug)]
pub enum ServerError {
    Protocol(ProtocolError),
    AuthenticationRequired,
    AuthenticationFailed,
    WorkerDisconnected,
    WorkerPanicked,
    WriterPanicked,
}

impl fmt::Display for ServerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Protocol(error) => write!(formatter, "{error}"),
            Self::AuthenticationRequired => write!(formatter, "first frame must be Hello"),
            Self::AuthenticationFailed => write!(formatter, "host authentication failed"),
            Self::WorkerDisconnected => write!(formatter, "engine worker disconnected"),
            Self::WorkerPanicked => write!(formatter, "engine worker panicked"),
            Self::WriterPanicked => write!(formatter, "protocol writer panicked"),
        }
    }
}

impl Error for ServerError {}

impl From<ProtocolError> for ServerError {
    fn from(value: ProtocolError) -> Self {
        Self::Protocol(value)
    }
}

struct WorkItem {
    request_id: u32,
    command: ClientCommand,
}

struct Runtime {
    // Field order is intentional: the engine unloads the DLL before its
    // temporary directory is removed.
    engine: EciEngine,
    _assets: PreparedEci,
    data_directory: PathBuf,
    language_code: String,
}

impl Runtime {
    fn initialize(
        config: InitializeConfig,
        events: SyncSender<EngineEvent>,
    ) -> Result<Self, String> {
        let data_directory = PathBuf::from(&config.data_directory);
        let assets = PreparedEci::create(Path::new(&config.eci_path), &data_directory)
            .map_err(|error| error.to_string())?;
        let api = EciApi::load(assets.dll_path().as_os_str()).map_err(|error| error.to_string())?;
        let mut engine =
            EciEngine::new(api, config.language_id, events).map_err(|error| error.to_string())?;
        engine.set_param(ECI_INPUT_TYPE, 1);
        engine.set_param(
            ECI_ABBREVIATION_DICTIONARY,
            i32::from(config.enable_abbreviation_dictionary),
        );
        engine.set_param(
            ECI_PHRASE_PREDICTION,
            i32::from(config.enable_phrase_prediction),
        );
        engine
            .load_dictionaries(&config.language_code, &data_directory)
            .map_err(|error| error.to_string())?;
        if config.voice_variant != 0 {
            engine.copy_voice(config.voice_variant);
        }
        Ok(Self {
            engine,
            _assets: assets,
            data_directory,
            language_code: config.language_code,
        })
    }

    fn execute(&mut self, command: ClientCommand) -> Result<Vec<u8>, String> {
        match command {
            ClientCommand::BeginGeneration(generation) => {
                self.engine.begin_generation(generation);
                Ok(Vec::new())
            }
            ClientCommand::AddText(text) => self
                .engine
                .add_text(&text)
                .map(|()| Vec::new())
                .map_err(|error| error.to_string()),
            ClientCommand::InsertIndex(index) => self
                .engine
                .insert_index(index)
                .map(|()| Vec::new())
                .map_err(|error| error.to_string()),
            ClientCommand::Synthesize => self
                .engine
                .synthesize()
                .map(|()| Vec::new())
                .map_err(|error| error.to_string()),
            ClientCommand::SetParam { parameter, value } => {
                self.engine.set_param(parameter, value);
                if parameter == ECI_LANGUAGE_DIALECT {
                    self.language_code = language_code_for_id(value).to_owned();
                    self.engine
                        .load_dictionaries(&self.language_code, &self.data_directory)
                        .map_err(|error| error.to_string())?;
                }
                Ok(self.state_payload())
            }
            ClientCommand::SetVoiceParam { parameter, value } => {
                self.engine.set_voice_param(parameter, value);
                Ok(self.state_payload())
            }
            ClientCommand::CopyVoice(variant) => {
                self.engine.copy_voice(variant);
                Ok(self.state_payload())
            }
            _ => Err("command is not valid on the engine worker".to_owned()),
        }
    }

    fn state_payload(&self) -> Vec<u8> {
        let mut payload = crate::protocol::PayloadWriter::new();
        payload.put_u32(2);
        for parameter in [ECI_INPUT_TYPE, ECI_LANGUAGE_DIALECT] {
            payload.put_i32(parameter);
            payload.put_i32(self.engine.get_param(parameter));
        }
        payload.put_u32(7);
        for parameter in 1..=7 {
            payload.put_i32(parameter);
            payload.put_i32(self.engine.get_voice_param(parameter));
        }
        payload.finish()
    }
}

pub fn run<R, W>(
    expected_key: [u8; AUTH_KEY_LEN],
    mut input: R,
    output: W,
) -> Result<(), ServerError>
where
    R: Read,
    W: Write + Send + 'static,
{
    let (outbound_tx, outbound_rx) = mpsc::sync_channel::<Frame>(QUEUE_CAPACITY);
    let writer = thread::spawn(move || write_frames(output, outbound_rx));

    let authenticated = authenticate(&expected_key, &mut input, &outbound_tx);
    if let Err(error) = authenticated {
        drop(outbound_tx);
        let _ = writer.join();
        return Err(error);
    }

    let stop_controller = Arc::new(Mutex::new(None));
    let (work_tx, work_rx) = mpsc::sync_channel(QUEUE_CAPACITY);
    let worker_outbound = outbound_tx.clone();
    let worker_stop = Arc::clone(&stop_controller);
    let worker = thread::spawn(move || engine_worker(work_rx, worker_outbound, worker_stop));

    while let Some(frame) = Frame::read_from(&mut input)? {
        let request_id = frame.request_id;
        let command = match ClientCommand::decode(&frame) {
            Ok(command) => command,
            Err(error) => {
                send_error(&outbound_tx, request_id, &error.to_string());
                continue;
            }
        };
        match command {
            ClientCommand::Stop => {
                let controller = stop_controller
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .clone();
                if controller.is_some_and(|controller| controller.request_stop()) {
                    send_frame(&outbound_tx, wire::response(request_id));
                } else {
                    send_error(&outbound_tx, request_id, "engine is not initialized");
                }
            }
            ClientCommand::Hello(_) => {
                send_error(&outbound_tx, request_id, "Hello may only be sent once");
            }
            ClientCommand::Delete => {
                work_tx
                    .send(WorkItem {
                        request_id,
                        command: ClientCommand::Delete,
                    })
                    .map_err(|_| ServerError::WorkerDisconnected)?;
                break;
            }
            command => work_tx
                .send(WorkItem {
                    request_id,
                    command,
                })
                .map_err(|_| ServerError::WorkerDisconnected)?,
        }
    }

    drop(work_tx);
    worker.join().map_err(|_| ServerError::WorkerPanicked)?;
    drop(outbound_tx);
    writer
        .join()
        .map_err(|_| ServerError::WriterPanicked)?
        .map_err(ServerError::Protocol)
}

fn authenticate(
    expected_key: &[u8; AUTH_KEY_LEN],
    input: &mut impl Read,
    outbound: &SyncSender<Frame>,
) -> Result<(), ServerError> {
    let frame = Frame::read_from(input)?.ok_or(ServerError::AuthenticationRequired)?;
    let request_id = frame.request_id;
    let ClientCommand::Hello(actual_key) = ClientCommand::decode(&frame)? else {
        send_error(outbound, request_id, "first frame must be Hello");
        return Err(ServerError::AuthenticationRequired);
    };
    let difference = actual_key
        .iter()
        .zip(expected_key)
        .fold(0_u8, |difference, (actual, expected)| {
            difference | (actual ^ expected)
        });
    if difference != 0 {
        send_error(outbound, request_id, "host authentication failed");
        return Err(ServerError::AuthenticationFailed);
    }
    send_frame(
        outbound,
        Frame::new(MessageKind::HelloAck, request_id, Vec::new()),
    );
    Ok(())
}

fn engine_worker(
    work: Receiver<WorkItem>,
    outbound: SyncSender<Frame>,
    shared_stop: Arc<Mutex<Option<StopController>>>,
) {
    let (event_tx, event_rx) = mpsc::sync_channel(QUEUE_CAPACITY);
    let event_outbound = outbound.clone();
    let event_forwarder = thread::spawn(move || {
        while let Ok(event) = event_rx.recv() {
            let Ok(frame) = wire::engine_event(event) else {
                break;
            };
            if event_outbound.send(frame).is_err() {
                break;
            }
        }
    });
    let mut runtime: Option<Runtime> = None;

    while let Ok(item) = work.recv() {
        let result: Result<Vec<u8>, String> = match item.command {
            ClientCommand::Initialize(config) if runtime.is_none() => {
                Runtime::initialize(config, event_tx.clone()).map(|created| {
                    let state = created.state_payload();
                    *shared_stop
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner()) =
                        Some(created.engine.stop_controller());
                    runtime = Some(created);
                    state
                })
            }
            ClientCommand::Initialize(_) => Err("engine is already initialized".to_owned()),
            ClientCommand::Delete => {
                send_frame(&outbound, wire::response(item.request_id));
                break;
            }
            command => runtime
                .as_mut()
                .ok_or_else(|| "engine is not initialized".to_owned())
                .and_then(|runtime| runtime.execute(command)),
        };
        match result {
            Ok(payload) => send_frame(
                &outbound,
                wire::response_with_payload(item.request_id, payload),
            ),
            Err(error) => send_error(&outbound, item.request_id, &error),
        }
    }

    *shared_stop
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner()) = None;
    drop(runtime);
    drop(event_tx);
    let _ = event_forwarder.join();
}

fn write_frames(mut output: impl Write, frames: Receiver<Frame>) -> Result<(), ProtocolError> {
    while let Ok(frame) = frames.recv() {
        frame.write_to(&mut output)?;
    }
    Ok(())
}

fn send_frame(outbound: &SyncSender<Frame>, frame: Frame) {
    let _ = outbound.send(frame);
}

fn send_error(outbound: &SyncSender<Frame>, request_id: u32, message: &str) {
    if let Ok(frame) = wire::error_response(request_id, message) {
        send_frame(outbound, frame);
    }
}

fn language_code_for_id(language_id: i32) -> &'static str {
    match language_id {
        131_073 => "esm",
        131_072 => "esp",
        458_752 => "ptb",
        196_609 => "frc",
        196_608 => "fra",
        589_824 => "fin",
        262_144 => "deu",
        327_680 => "ita",
        65_537 => "eng",
        393_216 => "chs",
        524_288 => "jpn",
        655_360 => "kor",
        _ => "enu",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::{PayloadReader, PayloadWriter};

    fn hello(key: [u8; AUTH_KEY_LEN], request_id: u32) -> Frame {
        let mut payload = PayloadWriter::new();
        payload.put_bytes(&key).unwrap();
        Frame::new(MessageKind::Hello, request_id, payload.finish())
    }

    #[test]
    fn authentication_accepts_the_expected_key() {
        let key = [7; AUTH_KEY_LEN];
        let encoded = hello(key, 12).encode().unwrap();
        let mut input = encoded.as_slice();
        let (outbound, receiver) = mpsc::sync_channel(2);
        authenticate(&key, &mut input, &outbound).unwrap();
        assert_eq!(receiver.recv().unwrap().kind, MessageKind::HelloAck);
    }

    #[test]
    fn authentication_rejects_a_different_key() {
        let encoded = hello([8; AUTH_KEY_LEN], 12).encode().unwrap();
        let mut input = encoded.as_slice();
        let (outbound, receiver) = mpsc::sync_channel(2);
        assert!(matches!(
            authenticate(&[7; AUTH_KEY_LEN], &mut input, &outbound),
            Err(ServerError::AuthenticationFailed)
        ));
        let frame = receiver.recv().unwrap();
        assert_eq!(frame.kind, MessageKind::ErrorResponse);
        let mut payload = PayloadReader::new(&frame.payload);
        assert!(payload.get_string().unwrap().contains("authentication"));
    }

    #[test]
    fn language_mapping_matches_existing_host_ids() {
        assert_eq!(language_code_for_id(65_536), "enu");
        assert_eq!(language_code_for_id(131_073), "esm");
        assert_eq!(language_code_for_id(655_360), "kor");
    }
}
