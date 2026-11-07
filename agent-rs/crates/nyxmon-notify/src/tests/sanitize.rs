//! Handwritten security invariants independent of generated fixtures.
use crate::sanitize::{sanitize_untrusted_data, sanitize_untrusted_dict, sanitize_untrusted_text};
use nyxmon_core::pyjson::{PyDict, PyJson};

#[test]
fn controls_and_separators_are_replaced_but_safe_unicode_survives() {
    let hostile =
        "\0\t\r\n\u{1b}\u{7f}\u{85}\u{ad}\u{200b}\u{200e}\u{202e}\u{2066}\u{feff}\u{2028}\u{2029}";
    assert_eq!(sanitize_untrusted_text(hostile, None), "?".repeat(15));
    let safe = "Grüße 漢字 🙂 e\u{301} [brackets]";
    assert_eq!(sanitize_untrusted_text(safe, None), safe);
    let mut map = PyDict::new();
    map.insert(
        hostile.to_owned(),
        PyJson::List(vec![PyJson::from(hostile), PyJson::from(safe)]),
    );
    let mut expected = PyDict::new();
    expected.insert(
        "?".repeat(15),
        PyJson::List(vec![PyJson::from("?".repeat(15)), PyJson::from(safe)]),
    );
    assert_eq!(sanitize_untrusted_dict(&map), PyJson::Dict(expected));
}

#[test]
fn strings_and_keys_keep_exactly_two_hundred_codepoints() {
    let edge = "界".repeat(200);
    assert_eq!(
        sanitize_untrusted_data(&PyJson::from(edge.clone())),
        PyJson::from(edge.clone())
    );
    let long = format!("{edge}\n🙂");
    let truncated = format!("{edge}...[truncated 2 chars]");
    assert_eq!(
        sanitize_untrusted_data(&PyJson::from(long.clone())),
        PyJson::from(truncated.clone())
    );
    let mut map = PyDict::new();
    map.insert(edge.clone(), PyJson::from("edge"));
    map.insert(long, PyJson::from("long"));
    let mut expected = PyDict::new();
    expected.insert(edge, PyJson::from("edge"));
    expected.insert(truncated, PyJson::from("long"));
    assert_eq!(sanitize_untrusted_dict(&map), PyJson::Dict(expected));
    assert_eq!(
        sanitize_untrusted_text("\n界", Some(1)),
        "?...[truncated 1 chars]"
    );
}

#[test]
fn lists_and_dicts_keep_twenty_five_entries_and_count_omissions() {
    for count in [24, 25, 26, 28] {
        let input: Vec<PyJson> = (0..count).map(PyJson::Int).collect();
        let mut expected: Vec<PyJson> = (0..count.min(25)).map(PyJson::Int).collect();
        if count > 25 {
            expected.push(PyJson::from(format!("[{} more items omitted]", count - 25)));
        }
        assert_eq!(
            sanitize_untrusted_data(&PyJson::List(input)),
            PyJson::List(expected)
        );
        let mut map = PyDict::new();
        let mut expected = PyDict::new();
        for i in 0..count {
            map.insert(format!("key{i}"), PyJson::Int(i));
            if i < 25 {
                expected.insert(format!("key{i}"), PyJson::Int(i));
            }
        }
        if count > 25 {
            expected.insert(
                "[omitted]".to_owned(),
                PyJson::from(format!("{} more entries", count - 25)),
            );
        }
        assert_eq!(sanitize_untrusted_dict(&map), PyJson::Dict(expected));
    }
}

#[test]
fn containers_stop_at_depth_four_but_scalars_at_that_depth_survive() {
    for dict in [false, true] {
        let wrap = |value| {
            if dict {
                let mut map = PyDict::new();
                map.insert("key".to_owned(), value);
                PyJson::Dict(map)
            } else {
                PyJson::List(vec![value])
            }
        };
        let nest = |mut value, depth| {
            for _ in 0..depth {
                value = wrap(value);
            }
            value
        };
        for depth in [3, 4] {
            let input = nest(PyJson::from("leaf\n"), depth);
            assert_eq!(
                sanitize_untrusted_data(&input),
                nest(PyJson::from("leaf?"), depth)
            );
        }
        let input = nest(PyJson::from("leaf"), 5);
        assert_eq!(
            sanitize_untrusted_data(&input),
            nest(PyJson::from("[nested data omitted]"), 4)
        );
        let empty_at_four = nest(
            if dict {
                PyJson::dict()
            } else {
                PyJson::List(vec![])
            },
            4,
        );
        assert_eq!(
            sanitize_untrusted_data(&empty_at_four),
            nest(PyJson::from("[nested data omitted]"), 4)
        );
    }
}

#[test]
fn colliding_keys_replace_in_place_and_omissions_count_input_entries() {
    let mut map = PyDict::new();
    map.insert("a\n".to_owned(), PyJson::Int(1));
    map.insert("middle".to_owned(), PyJson::Int(2));
    map.insert("a\r".to_owned(), PyJson::Int(3));
    let prefix = "界".repeat(200);
    map.insert(format!("{prefix}x"), PyJson::Int(4));
    map.insert(format!("{prefix}y"), PyJson::Int(5));
    map.insert("[omitted]".to_owned(), PyJson::from("untrusted marker"));
    for i in 6..28 {
        map.insert(format!("key{i}"), PyJson::Int(i));
    }
    let mut expected = PyDict::new();
    expected.insert("a?".to_owned(), PyJson::Int(3));
    expected.insert("middle".to_owned(), PyJson::Int(2));
    expected.insert(format!("{prefix}...[truncated 1 chars]"), PyJson::Int(5));
    expected.insert("[omitted]".to_owned(), PyJson::from("3 more entries"));
    for i in 6..25 {
        expected.insert(format!("key{i}"), PyJson::Int(i));
    }
    assert_eq!(sanitize_untrusted_dict(&map), PyJson::Dict(expected));
}
