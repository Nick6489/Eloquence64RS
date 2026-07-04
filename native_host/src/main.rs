use eloquence_native_host::protocol::AUTH_KEY_LEN;
use eloquence_native_host::server;

fn main() {
    if cfg!(not(target_pointer_width = "32")) {
        eprintln!("eloquence_host32_native must be built for a 32-bit target");
        std::process::exit(2);
    }
    let key = match authentication_key(std::env::args().skip(1)) {
        Ok(key) => key,
        Err(message) => {
            eprintln!("{message}");
            eprintln!("usage: eloquence_host32_native --auth-key <32 hex digits>");
            std::process::exit(2);
        }
    };
    if let Err(error) = server::run(key, std::io::stdin(), std::io::stdout()) {
        eprintln!("native Eloquence host failed: {error}");
        std::process::exit(1);
    }
}

fn authentication_key(
    mut arguments: impl Iterator<Item = String>,
) -> Result<[u8; AUTH_KEY_LEN], String> {
    if arguments.next().as_deref() != Some("--auth-key") {
        return Err("missing --auth-key".to_owned());
    }
    let encoded = arguments
        .next()
        .ok_or_else(|| "missing authentication key".to_owned())?;
    if arguments.next().is_some() {
        return Err("unexpected command-line arguments".to_owned());
    }
    if encoded.len() != AUTH_KEY_LEN * 2 {
        return Err("authentication key must contain 32 hex digits".to_owned());
    }
    let mut key = [0_u8; AUTH_KEY_LEN];
    for (index, byte) in key.iter_mut().enumerate() {
        let start = index * 2;
        *byte = u8::from_str_radix(&encoded[start..start + 2], 16)
            .map_err(|_| "authentication key contains non-hexadecimal text".to_owned())?;
    }
    Ok(key)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn command_line_key_parser_is_strict() {
        assert_eq!(
            authentication_key(
                ["--auth-key", "00112233445566778899aabbccddeeff"]
                    .into_iter()
                    .map(str::to_owned)
            )
            .unwrap(),
            [
                0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88, 0x99, 0xaa, 0xbb, 0xcc, 0xdd,
                0xee, 0xff,
            ]
        );
        assert!(authentication_key(std::iter::empty()).is_err());
        assert!(authentication_key(
            ["--auth-key", "not-hex-not-hex-not-hex-not-hex!!"]
                .into_iter()
                .map(str::to_owned)
        )
        .is_err());
    }
}
