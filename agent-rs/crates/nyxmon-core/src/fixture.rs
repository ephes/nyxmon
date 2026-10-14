//! Access to the Python-generated fixtures (`agent-rs/fixtures`) in tests.

use crate::pyjson::{PyJson, loads};

/// Parse a fixture file with the crate's own parser.
pub(crate) fn load(text: &str) -> PyJson {
    loads(text).expect("fixture file is valid JSON")
}

/// Field accessors that panic with the field name on malformed fixtures.
pub(crate) trait Field {
    fn field(&self, key: &str) -> &PyJson;
    fn str_field(&self, key: &str) -> &str;
    fn opt_str(&self, key: &str) -> Option<&str>;
    fn list_field(&self, key: &str) -> &[PyJson];
    fn bool_field(&self, key: &str) -> bool;
    fn int_field(&self, key: &str) -> i64;
}

impl Field for PyJson {
    fn field(&self, key: &str) -> &PyJson {
        self.get(key)
            .unwrap_or_else(|| panic!("fixture field {key:?} missing in {self:?}"))
    }

    fn str_field(&self, key: &str) -> &str {
        self.field(key)
            .as_str()
            .unwrap_or_else(|| panic!("fixture field {key:?} is not a string"))
    }

    fn opt_str(&self, key: &str) -> Option<&str> {
        match self.field(key) {
            PyJson::Null => None,
            value => Some(value.as_str().expect("string or null")),
        }
    }

    fn list_field(&self, key: &str) -> &[PyJson] {
        self.field(key)
            .as_list()
            .unwrap_or_else(|| panic!("fixture field {key:?} is not a list"))
    }

    fn bool_field(&self, key: &str) -> bool {
        self.field(key)
            .as_bool()
            .unwrap_or_else(|| panic!("fixture field {key:?} is not a bool"))
    }

    fn int_field(&self, key: &str) -> i64 {
        self.field(key)
            .as_i64()
            .unwrap_or_else(|| panic!("fixture field {key:?} is not an int"))
    }
}

pub(crate) fn hex_bytes(hex: &str) -> Vec<u8> {
    (0..hex.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).expect("hex"))
        .collect()
}
