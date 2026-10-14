//! TLS helpers against a local rustls server with `rcgen` certificates.

use std::net::{IpAddr, Ipv4Addr};
use std::sync::Arc;

use nyxmon_core::log::RecordingSink;
use rcgen::{BasicConstraints, CertificateParams, CertifiedIssuer, IsCa, KeyPair, SanType};
use rustls::pki_types::{PrivateKeyDer, PrivatePkcs8KeyDer};
use rustls::{RootCertStore, ServerConfig};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio_rustls::{TlsAcceptor, TlsConnector};

use super::*;

struct Pki {
    ca_der: CertificateDer<'static>,
    server: Arc<ServerConfig>,
}

/// A CA and a leaf for `localhost` and `127.0.0.1` signed by it.
fn pki() -> Pki {
    let mut ca_params = CertificateParams::new(Vec::<String>::new()).unwrap();
    ca_params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
    let ca = CertifiedIssuer::self_signed(ca_params, KeyPair::generate().unwrap()).unwrap();

    let leaf_key = KeyPair::generate().unwrap();
    let mut leaf_params = CertificateParams::new(vec!["localhost".to_owned()]).unwrap();
    leaf_params
        .subject_alt_names
        .push(SanType::IpAddress(IpAddr::V4(Ipv4Addr::LOCALHOST)));
    let leaf = leaf_params.signed_by(&leaf_key, &ca).unwrap();

    let key = PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(leaf_key.serialize_der()));
    let server = ServerConfig::builder_with_provider(crypto_provider())
        .with_safe_default_protocol_versions()
        .unwrap()
        .with_no_client_auth()
        .with_single_cert(vec![leaf.der().clone()], key)
        .unwrap();
    Pki {
        ca_der: ca.der().clone(),
        server: Arc::new(server),
    }
}

/// Serve one TLS connection that echoes a single read, then connect to it
/// with `config` verifying `name`. Returns the client's handshake outcome.
async fn handshake(
    server: Arc<ServerConfig>,
    config: Arc<ClientConfig>,
    name: &str,
) -> Result<(), String> {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let acceptor = TlsAcceptor::from(server);
    let serve = tokio::spawn(async move {
        let (socket, _) = listener.accept().await.unwrap();
        if let Ok(mut tls) = acceptor.accept(socket).await {
            let mut buffer = [0u8; 4];
            if tls.read_exact(&mut buffer).await.is_ok() {
                let _ = tls.write_all(&buffer).await;
            }
        }
    });
    let stream = TcpStream::connect(address).await.unwrap();
    let connector = TlsConnector::from(config);
    let outcome = async {
        let mut tls = connector
            .connect(server_name(name).map_err(|e| e.to_string())?, stream)
            .await
            .map_err(|e| e.to_string())?;
        tls.write_all(b"ping").await.map_err(|e| e.to_string())?;
        let mut reply = [0u8; 4];
        tls.read_exact(&mut reply)
            .await
            .map_err(|e| e.to_string())?;
        assert_eq!(&reply, b"ping");
        Ok(())
    }
    .await;
    serve.abort();
    outcome
}

fn roots_with(ca: &CertificateDer<'static>) -> Arc<RootCertStore> {
    let mut roots = RootCertStore::empty();
    roots.add(ca.clone()).unwrap();
    Arc::new(roots)
}

#[tokio::test]
async fn verifying_config_accepts_a_trusted_chain_by_name_and_ip() {
    let pki = pki();
    let config = verifying_client_config(roots_with(&pki.ca_der));
    handshake(pki.server.clone(), config.clone(), "localhost")
        .await
        .unwrap();
    handshake(pki.server.clone(), config, "127.0.0.1")
        .await
        .unwrap();
}

#[tokio::test]
async fn verifying_config_rejects_unknown_issuer_and_wrong_name() {
    let pki = pki();
    let untrusted = verifying_client_config(Arc::new(RootCertStore::empty()));
    let err = handshake(pki.server.clone(), untrusted, "localhost")
        .await
        .unwrap_err();
    assert!(err.contains("UnknownIssuer"), "{err}");

    let trusted = verifying_client_config(roots_with(&pki.ca_der));
    let err = handshake(pki.server.clone(), trusted, "example.invalid")
        .await
        .unwrap_err();
    assert!(err.contains("not valid for name"), "{err}");
}

#[tokio::test]
async fn unverified_config_accepts_any_chain_and_name() {
    let pki = pki();
    handshake(
        pki.server.clone(),
        unverified_client_config(),
        "example.invalid",
    )
    .await
    .unwrap();
    handshake(pki.server, client_config(false), "127.0.0.1")
        .await
        .unwrap();
}

#[test]
fn mail_config_warns_only_without_verification() {
    let sink = RecordingSink::default();
    build_client_tls_config(true, "SMTP", "mail.example", &sink);
    assert!(sink.take().is_empty());
    build_client_tls_config(false, "IMAP", "imap.example", &sink);
    assert_eq!(
        sink.take(),
        vec![(
            "nyxmon.adapters.runner.executors.tls".to_owned(),
            "IMAP check for imap.example runs with TLS certificate verification disabled \
             (verify=false); credentials are exposed to man-in-the-middle attacks"
                .to_owned()
        )]
    );
}

#[test]
fn server_names_distinguish_ip_literals_from_dns_names() {
    assert!(matches!(
        server_name("127.0.0.1"),
        Ok(ServerName::IpAddress(_))
    ));
    assert!(matches!(server_name("::1"), Ok(ServerName::IpAddress(_))));
    assert!(matches!(
        server_name("mail.example.com"),
        Ok(ServerName::DnsName(_))
    ));
    assert!(server_name("not a host").is_err());
}

#[test]
fn only_tls12_and_tls13_are_offered() {
    let config = client_config(false);
    let mut versions: Vec<_> = config
        .crypto_provider()
        .cipher_suites
        .iter()
        .map(|suite| suite.version().version)
        .collect();
    versions.sort_by_key(|v| u16::from(*v));
    versions.dedup();
    assert_eq!(
        versions,
        vec![
            rustls::ProtocolVersion::TLSv1_2,
            rustls::ProtocolVersion::TLSv1_3
        ]
    );
}

#[test]
fn idna_encoding_matches_python() {
    let fixture = nyxmon_core::pyjson::loads(include_str!("../../../../fixtures/idna.json"))
        .expect("fixture");
    let cases = fixture
        .get("cases")
        .and_then(|c| c.as_list())
        .expect("cases");
    assert!(cases.len() >= 15);
    for case in cases {
        let host = case.get("host").and_then(|h| h.as_str()).unwrap();
        let expected = case.get("ascii").and_then(|a| a.as_str());
        match (idna_encode(host), expected) {
            (Ok(ascii), Some(expected)) => assert_eq!(ascii, expected, "{host:?}"),
            (Err(_), None) => {}
            (outcome, expected) => panic!("{host:?}: {outcome:?} vs Python {expected:?}"),
        }
    }
}

#[test]
fn unicode_host_names_become_dns_names() {
    let name = server_name("b\u{fc}cher.example").unwrap();
    assert_eq!(name.to_str(), "xn--bcher-kva.example");
}
