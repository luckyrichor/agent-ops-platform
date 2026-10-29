"""Opt-in local evidence: redacted fields plus an encrypted, exact replay payload."""
import base64
import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent_ops.payment_gateway import PaymentError, canonical


class NoticeArchive:
    def __init__(self, folder: Path) -> None:
        root = (Path.cwd()/'.local').resolve()
        if folder.is_symlink() or not folder.resolve().is_relative_to(root):
            raise PaymentError('PAYMENT_NOTICE_ARCHIVE_UNAVAILABLE')
        self.folder = folder.resolve()
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if stat.S_IMODE(self.folder.stat().st_mode) != 0o700:
            raise PaymentError('PAYMENT_NOTICE_ARCHIVE_UNAVAILABLE')

    def _key(self) -> bytes:
        path = self.folder/'archive.key'
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as output:
                output.write(AESGCM.generate_key(bit_length=256))
                output.flush()
                os.fsync(output.fileno())
        if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ValueError
        key = path.read_bytes()
        if len(key) != 32:
            raise ValueError
        return key

    def capture(self, params: dict[str, str]) -> None:
        """Caller must have verified and applied the notification before calling."""
        try:
            digest = hashlib.sha256(canonical(params, notification=True)).hexdigest()
            path = self.folder/f'notice-{digest}.json'
            if path.exists() or path.is_symlink():
                # Validate the existing encrypted evidence before acknowledging a replay.
                if self.load(path) != params:
                    raise ValueError
                return
            nonce = os.urandom(12)
            encrypted = AESGCM(self._key()).encrypt(nonce, json.dumps(params).encode(), b'payment-notice-v1')
            record = {'version':1, 'received_at':datetime.now(UTC).isoformat(),
                      'signature_verified':True, 'fields':{k:'<redacted>' for k in params},
                      'nonce':base64.b64encode(nonce).decode(),
                      'ciphertext':base64.b64encode(encrypted).decode()}
            # Publish only a complete, durable file. Concurrent captures are equivalent.
            temporary = self.folder/f'.notice-{os.urandom(16).hex()}.tmp'
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as output:
                json.dump(record, output)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        except (OSError, ValueError, TypeError):
            raise PaymentError('PAYMENT_NOTICE_ARCHIVE_UNAVAILABLE') from None

    def load(self, path: Path) -> dict[str, str]:
        try:
            if path.is_symlink() or path.resolve().parent != self.folder or stat.S_IMODE(path.stat().st_mode) != 0o600 or path.stat().st_size > 1024*1024:
                raise ValueError
            record = json.loads(path.read_text())
            if record['version'] != 1:
                raise ValueError
            raw = AESGCM(self._key()).decrypt(base64.b64decode(record['nonce'], validate=True),
                base64.b64decode(record['ciphertext'], validate=True), b'payment-notice-v1')
            params = json.loads(raw)
            if not isinstance(params, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in params.items()):
                raise ValueError
            return params
        except (OSError, ValueError, TypeError, KeyError, InvalidTag):
            raise PaymentError('PAYMENT_NOTICE_ARCHIVE_UNAVAILABLE') from None
