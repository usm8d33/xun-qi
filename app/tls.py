"""產生 LocalSend 需要的自簽憑證。指紋 = 憑證 DER 的 SHA-256（大寫十六進位），與官方 LocalSend 相同。"""
import datetime
import hashlib
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def ensure_cert(folder: Path, name: str):
    """回傳 (cert_path, key_path, fingerprint)。每台虛擬裝置一組憑證，第一次執行時產生後就固定使用。"""
    folder.mkdir(parents=True, exist_ok=True)
    cert_p, key_p = folder / f"{name}.crt", folder / f"{name}.key"
    if not (cert_p.exists() and key_p.exists()):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "LocalSend User")])
        now = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subj)
            .issuer_name(subj)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(datetime.datetime(2099, 12, 31, tzinfo=datetime.timezone.utc))
            .sign(key, hashes.SHA256())
        )
        key_p.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                            serialization.PrivateFormat.TraditionalOpenSSL,
                                            serialization.NoEncryption()))
        cert_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    cert = x509.load_pem_x509_certificate(cert_p.read_bytes())
    fp = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest().upper()
    return cert_p, key_p, fp
