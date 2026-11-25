from cryptography.fernet import Fernet


class SecretBox:
    """Encrypt exact bytes. No stripping or implicit GS/code normalization."""

    def __init__(self, key: str):
        self._cipher = Fernet(key.encode("ascii"))

    def seal(self, value: bytes) -> bytes:
        return self._cipher.encrypt(value)

    def open(self, value: bytes) -> bytes:
        return self._cipher.decrypt(value)
