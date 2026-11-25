import base64
import re
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from pack_v2.shared.errors import DomainError


def create():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Pack v2 local printing")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    return (
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
        certificate.public_bytes(serialization.Encoding.PEM).decode(),
    )


def sign(key, value):
    if not re.fullmatch("[0-9a-f]{64}", value):
        raise DomainError("Подписываются только SHA-256 запросов QZ Tray")
    private = serialization.load_pem_private_key(key, password=None)
    return base64.b64encode(private.sign(value.encode("ascii"), padding.PKCS1v15(), hashes.SHA512())).decode()
