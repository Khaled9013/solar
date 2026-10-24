"""Create a tiny, deterministic two-page PDF for parser and browser acceptance."""
from pathlib import Path


def document():
    stream1 = b"BT /F1 24 Tf 72 720 Td (Solar PDF preview) Tj ET"
    stream2 = b"BT /F1 24 Tf 72 720 Td (Second page) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 7 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream1)} >>\nstream\n".encode() + stream1 + b"\nendstream",
        f"<< /Length {len(stream2)} >>\nstream\n".encode() + stream2 + b"\nendstream",
    ]
    data = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, value in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{i} 0 obj\n".encode() + value + b"\nendobj\n")
    start = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        data.extend(f"{offset:010} 00000 n \n".encode())
    data.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(data)


if __name__ == "__main__":
    Path(__file__).with_name("fixtures").joinpath("preview.pdf").write_bytes(document())
