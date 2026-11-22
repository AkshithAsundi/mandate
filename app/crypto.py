"""Ed25519 server key: generated on first run, stored as PEM in a git-ignored file."""
import base64
import json
import os
from functools import lru_cache

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from . import config


def canonical_json(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@lru_cache(maxsize=8)
def _load_or_create(path: str) -> Ed25519PrivateKey:
    if os.path.exists(path):
        with open(path, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise RuntimeError(f"{path} is not an Ed25519 private key")
        return key
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    # O_EXCL so two processes starting at once can't overwrite each other's key.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    return key


def private_key() -> Ed25519PrivateKey:
    return _load_or_create(config.key_path())


def public_key() -> Ed25519PublicKey:
    return private_key().public_key()


def public_key_b64() -> str:
    raw = public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def sign(body: str) -> str:
    return base64.b64encode(private_key().sign(body.encode())).decode()


def verify(body: str, signature_b64: str) -> bool:
    try:
        public_key().verify(base64.b64decode(signature_b64), body.encode())
        return True
    except (InvalidSignature, ValueError):
        return False
