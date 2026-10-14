//! Shared TLS helpers for check executors (`executors/tls.py`, and the
//! context of `tcp_executor.py:_build_ssl_context`).
//!
//! rustls replaces OpenSSL (PLAN.md D-9): TLS 1.2 and 1.3 only, the `ring`
//! provider, and the system trust store like `ssl.create_default_context()`.
//! With verification off the handshake still checks that the server owns the
//! key of the certificate it presented; only the chain and the name are not
//! checked, like `ssl.CERT_NONE` with `check_hostname = False`.

use std::fmt;
use std::sync::{Arc, OnceLock};

use nyxmon_core::log::{LogSink, loggers};
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::crypto::{CryptoProvider, WebPkiSupportedAlgorithms};
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::{ClientConfig, DigitallySignedStruct, RootCertStore, SignatureScheme};

/// The crypto provider of every agent TLS connection.
pub fn crypto_provider() -> Arc<CryptoProvider> {
    static PROVIDER: OnceLock<Arc<CryptoProvider>> = OnceLock::new();
    PROVIDER
        .get_or_init(|| Arc::new(rustls::crypto::ring::default_provider()))
        .clone()
}

/// The system trust store (`rustls-native-certs`), loaded once.
///
/// Certificates that fail to load or parse are skipped, like OpenSSL's
/// default verify paths; an empty store makes every verified handshake fail.
pub fn system_roots() -> Arc<RootCertStore> {
    static ROOTS: OnceLock<Arc<RootCertStore>> = OnceLock::new();
    ROOTS
        .get_or_init(|| {
            let mut roots = RootCertStore::empty();
            let loaded = rustls_native_certs::load_native_certs();
            roots.add_parsable_certificates(loaded.certs);
            Arc::new(roots)
        })
        .clone()
}

fn builder() -> rustls::ConfigBuilder<ClientConfig, rustls::WantsVerifier> {
    ClientConfig::builder_with_provider(crypto_provider())
        .with_safe_default_protocol_versions()
        .expect("the ring provider supports the default protocol versions")
}

/// A verifying client configuration trusting `roots`.
pub fn verifying_client_config(roots: Arc<RootCertStore>) -> Arc<ClientConfig> {
    Arc::new(
        builder()
            .with_root_certificates(roots)
            .with_no_client_auth(),
    )
}

/// A client configuration that accepts any server certificate.
pub fn unverified_client_config() -> Arc<ClientConfig> {
    let verifier = NoCertificateVerification {
        algorithms: crypto_provider().signature_verification_algorithms,
    };
    Arc::new(
        builder()
            .dangerous()
            .with_custom_certificate_verifier(Arc::new(verifier))
            .with_no_client_auth(),
    )
}

/// `ssl.create_default_context()` (`verify`) or an unverified context,
/// without a warning (the TCP check's `_build_ssl_context`).
pub fn client_config(verify: bool) -> Arc<ClientConfig> {
    if verify {
        verifying_client_config(system_roots())
    } else {
        unverified_client_config()
    }
}

/// `tls.py:build_client_ssl_context` for the mail checks: like
/// [`client_config`], and with verification off it warns that credentials
/// are exposed.
pub fn build_client_tls_config(
    verify: bool,
    check_type: &str,
    host: &str,
    log: &dyn LogSink,
) -> Arc<ClientConfig> {
    if !verify {
        log.warning(
            loggers::EXECUTOR_TLS,
            &format!(
                "{check_type} check for {host} runs with TLS certificate verification disabled \
                 (verify=false); credentials are exposed to man-in-the-middle attacks"
            ),
        );
    }
    client_config(verify)
}

/// Why a host cannot be used as a TLS server name.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ServerNameError(pub String);

impl fmt::Display for ServerNameError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for ServerNameError {}

/// `host.encode("idna")`: what Python's `ssl` (and `socket`) send for a
/// Unicode host name (IDNA 2003, see [`crate::pyidna`]).
///
/// # Errors
/// Every host for which Python raises `UnicodeError`.
pub fn idna_encode(host: &str) -> Result<String, ServerNameError> {
    crate::pyidna::encode(host).map_err(|err| ServerNameError(err.to_string()))
}

/// The name a connection to `host` is verified against. An IP literal
/// becomes an IP address name (no SNI is sent and IP SANs are checked, like
/// Python's `server_hostname` with an address); anything else a DNS name,
/// IDNA-encoded like Python's `ssl` module does ([`idna_encode`]).
///
/// # Errors
/// The host is neither an IP literal nor an encodable, valid DNS name.
pub fn server_name(host: &str) -> Result<ServerName<'static>, ServerNameError> {
    let ascii = idna_encode(host)?;
    ServerName::try_from(ascii).map_err(|err| ServerNameError(err.to_string()))
}

/// Accepts every certificate chain and name, but still checks the handshake
/// signatures against the presented certificate.
#[derive(Debug)]
struct NoCertificateVerification {
    algorithms: WebPkiSupportedAlgorithms,
}

impl ServerCertVerifier for NoCertificateVerification {
    fn verify_server_cert(
        &self,
        _end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp_response: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls12_signature(message, cert, dss, &self.algorithms)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls13_signature(message, cert, dss, &self.algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.algorithms.supported_schemes()
    }
}

#[cfg(test)]
mod tests;
