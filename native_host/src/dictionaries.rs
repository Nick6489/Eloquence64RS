//! Eloquence dictionary filename compatibility rules.

pub fn candidates(language_code: &str) -> [Vec<String>; 3] {
    let language_code = language_code.to_ascii_lowercase();
    let fallback = match language_code.as_str() {
        "eng" => Some("enu"),
        "esm" => Some("esp"),
        "frc" => Some("fra"),
        "chs" => Some("enu"),
        _ => None,
    };
    let mut codes = vec![language_code];
    if let Some(fallback) = fallback {
        codes.push(fallback.to_owned());
    }
    let include_generic = codes.iter().any(|code| code == "enu" || code == "eng");

    ["main", "root", "abbr"].map(|volume| {
        let mut names: Vec<_> = codes
            .iter()
            .map(|code| format!("{code}{volume}.dic"))
            .collect();
        if include_generic {
            names.push(format!("{volume}.dic"));
        }
        names
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn regional_and_generic_fallbacks_match_established_behavior() {
        assert_eq!(
            candidates("eng")[0],
            vec!["engmain.dic", "enumain.dic", "main.dic"]
        );
        assert_eq!(candidates("esm")[2], vec!["esmabbr.dic", "espabbr.dic"]);
        assert_eq!(
            candidates("chs")[1],
            vec!["chsroot.dic", "enuroot.dic", "root.dic"]
        );
        assert_eq!(candidates("deu")[0], vec!["deumain.dic"]);
    }
}
