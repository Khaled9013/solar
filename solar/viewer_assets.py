"""Install the pinned CAD WebAssembly runtime once, then serve it locally."""
import hashlib
import os
from pathlib import Path
import tempfile
import urllib.request

from filelock import FileLock

CAD_COMMIT = "41e470890ae0f9dc69ac50ffd5fc73e03576f4eb"
CAD_BLOB = "29458885e6b030850d352cff33ab0c22a7e36c35"
CAD_SIZE = 7_604_031
CAD_URL = f"https://raw.githubusercontent.com/kovacsv/occt-import-js/{CAD_COMMIT}/dist/occt-import-js.wasm"
CAD_PATH = Path(__file__).parent / "static" / "vendor" / "occt-import-js.wasm"


def valid_cad(data):
    return (len(data) == CAD_SIZE and data.startswith(b"\0asm")
            and hashlib.sha1(f"blob {len(data)}\0".encode() + data, usedforsecurity=False).hexdigest() == CAD_BLOB)


def ensure_cad_runtime():
    from solar.config import config
    with FileLock(str(config()["data_dir"] / "locks" / "viewer-assets.lock"), timeout=30):
        if CAD_PATH.is_file() and valid_cad(CAD_PATH.read_bytes()):
            return
        # A fixed HTTPS source and a Git object hash pin the exact upstream build.
        # No file, account or chat data is sent to the dependency server.
        request = urllib.request.Request(CAD_URL, headers={"User-Agent": "Solar-viewer-assets/1"})
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - fixed HTTPS URL
            data = response.read(CAD_SIZE + 1)
        if not valid_cad(data):
            raise ValueError("CAD viewer download did not match the pinned upstream build")
        descriptor, filename = tempfile.mkstemp(prefix=".cad-", dir=CAD_PATH.parent)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
            os.replace(filename, CAD_PATH)
        finally:
            Path(filename).unlink(missing_ok=True)
