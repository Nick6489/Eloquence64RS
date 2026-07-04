#![cfg(all(windows, target_pointer_width = "32"))]

use eloquence_native_host::progress::FINAL_INDEX;
use eloquence_native_host::protocol::{
    Frame, MessageKind, PayloadReader, PayloadWriter, AUTH_KEY_LEN,
};
use std::io::{BufReader, BufWriter};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

fn send(writer: &mut BufWriter<std::process::ChildStdin>, frame: Frame) {
    frame.write_to(writer).unwrap();
}

fn read(reader: &mut BufReader<std::process::ChildStdout>) -> Frame {
    Frame::read_from(reader)
        .unwrap()
        .expect("host closed stdout before the test completed")
}

fn await_response(
    reader: &mut BufReader<std::process::ChildStdout>,
    request_id: u32,
) -> Vec<Frame> {
    let mut preceding = Vec::new();
    loop {
        let frame = read(reader);
        if frame.request_id == request_id {
            assert_eq!(frame.kind, MessageKind::Response, "host returned an error");
            return preceding;
        }
        preceding.push(frame);
    }
}

#[test]
fn process_synthesizes_real_pcm_over_authenticated_stdio() {
    let Some(eci_path) = std::env::var_os("ELOQUENCE_ECI_PATH") else {
        return;
    };
    let eci_path = std::path::PathBuf::from(eci_path);
    let data_directory = eci_path.parent().unwrap();
    let key = [0x5a; AUTH_KEY_LEN];
    let key_hex = key
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();

    let mut child = Command::new(env!("CARGO_BIN_EXE_eloquence_host32"))
        .args(["--auth-key", &key_hex])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut writer = BufWriter::new(child.stdin.take().unwrap());
    let mut reader = BufReader::new(child.stdout.take().unwrap());

    let mut payload = PayloadWriter::new();
    payload.put_bytes(&key).unwrap();
    send(
        &mut writer,
        Frame::new(MessageKind::Hello, 1, payload.finish()),
    );
    let hello = read(&mut reader);
    assert_eq!(hello.kind, MessageKind::HelloAck);
    assert_eq!(hello.request_id, 1);

    let mut payload = PayloadWriter::new();
    payload.put_string(&eci_path.display().to_string()).unwrap();
    payload
        .put_string(&data_directory.display().to_string())
        .unwrap();
    payload.put_string("enu").unwrap();
    payload.put_i32(65_536);
    payload.put_u8(1);
    payload.put_u8(1);
    payload.put_i32(0);
    send(
        &mut writer,
        Frame::new(MessageKind::Initialize, 2, payload.finish()),
    );
    await_response(&mut reader, 2);

    let mut payload = PayloadWriter::new();
    payload.put_u64(9001);
    send(
        &mut writer,
        Frame::new(MessageKind::BeginGeneration, 3, payload.finish()),
    );
    await_response(&mut reader, 3);

    let mut payload = PayloadWriter::new();
    payload
        .put_bytes(b"Native host process integration test.")
        .unwrap();
    send(
        &mut writer,
        Frame::new(MessageKind::AddText, 4, payload.finish()),
    );
    await_response(&mut reader, 4);

    for (request_id, index) in [(5, 42), (6, FINAL_INDEX)] {
        let mut payload = PayloadWriter::new();
        payload.put_u32(index);
        send(
            &mut writer,
            Frame::new(MessageKind::InsertIndex, request_id, payload.finish()),
        );
        await_response(&mut reader, request_id);
    }

    send(
        &mut writer,
        Frame::new(MessageKind::Synthesize, 7, Vec::new()),
    );
    let mut events = Vec::new();
    let mut saw_response = false;
    let mut saw_done = false;
    while !saw_response || !saw_done {
        let frame = read(&mut reader);
        if frame.request_id == 7 {
            assert_eq!(frame.kind, MessageKind::Response, "host returned an error");
            saw_response = true;
        } else {
            saw_done |= frame.kind == MessageKind::Done;
            events.push(frame);
        }
    }
    assert!(events.iter().any(|frame| frame.kind == MessageKind::Audio));
    let index_position = events
        .iter()
        .position(|frame| {
            if frame.kind != MessageKind::Index {
                return false;
            }
            let mut payload = PayloadReader::new(&frame.payload);
            payload.get_u64().unwrap() == 9001 && payload.get_u32().unwrap() == 42
        })
        .expect("index event was not sent");
    let done_position = events
        .iter()
        .position(|frame| frame.kind == MessageKind::Done)
        .expect("done event was not sent");
    assert!(index_position < done_position);

    let mut payload = PayloadWriter::new();
    payload.put_u64(9002);
    send(
        &mut writer,
        Frame::new(MessageKind::BeginGeneration, 9, payload.finish()),
    );
    await_response(&mut reader, 9);

    let mut payload = PayloadWriter::new();
    payload.put_bytes(&b"one ".repeat(25_000)).unwrap();
    send(
        &mut writer,
        Frame::new(MessageKind::AddText, 10, payload.finish()),
    );
    await_response(&mut reader, 10);
    let mut payload = PayloadWriter::new();
    payload.put_u32(FINAL_INDEX);
    send(
        &mut writer,
        Frame::new(MessageKind::InsertIndex, 11, payload.finish()),
    );
    await_response(&mut reader, 11);

    let stop_started = Instant::now();
    send(
        &mut writer,
        Frame::new(MessageKind::Synthesize, 12, Vec::new()),
    );
    send(&mut writer, Frame::new(MessageKind::Stop, 13, Vec::new()));
    let mut saw_synthesize_response = false;
    let mut saw_stop_response = false;
    let mut saw_stopped = false;
    let mut saw_cancelled_done = false;
    while !saw_synthesize_response || !saw_stop_response || !saw_stopped {
        let frame = read(&mut reader);
        if frame.request_id == 12 {
            assert_eq!(frame.kind, MessageKind::Response);
            saw_synthesize_response = true;
        } else if frame.request_id == 13 {
            assert_eq!(frame.kind, MessageKind::Response);
            saw_stop_response = true;
        } else if matches!(frame.kind, MessageKind::Stopped | MessageKind::Done) {
            let mut payload = PayloadReader::new(&frame.payload);
            let generation = payload.get_u64().unwrap();
            if generation == 9002 {
                saw_stopped |= frame.kind == MessageKind::Stopped;
                saw_cancelled_done |= frame.kind == MessageKind::Done;
            }
        }
    }
    assert!(!saw_cancelled_done);
    assert!(
        stop_started.elapsed() < Duration::from_secs(2),
        "cancellation took {:?}",
        stop_started.elapsed()
    );

    send(&mut writer, Frame::new(MessageKind::Delete, 14, Vec::new()));
    await_response(&mut reader, 14);
    drop(writer);
    let status = child.wait().unwrap();
    if !status.success() {
        let stderr = child
            .stderr
            .take()
            .map(|mut stderr| {
                let mut text = String::new();
                std::io::Read::read_to_string(&mut stderr, &mut text).unwrap();
                text
            })
            .unwrap_or_default();
        panic!("host exited with {status}: {stderr}");
    }
}
