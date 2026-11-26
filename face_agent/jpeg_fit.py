"""Reduz um JPEG ate caber no limite do terminal (a linha Bio-T aceita so 100KB; o app e o
painel mandam ate 200KB)."""
import io

from .errors import FaceProvisioningError

MIN_SIDE = 480  # o menor lado nao desce disso: abaixo o terminal recusa o rosto


def fit_jpeg(jpeg: bytes, max_bytes: int) -> bytes:
    if len(jpeg) <= max_bytes:
        return jpeg
    try:
        from PIL import Image
    except ImportError as e:
        raise FaceProvisioningError(f"JPEG de {len(jpeg)} bytes acima de {max_bytes} e Pillow ausente para reduzir") from e
    try:
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    except Exception as e:
        raise FaceProvisioningError(f"JPEG de {len(jpeg)} bytes excede o limite de {max_bytes} bytes e nao abre para reduzir") from e
    scale = 1.0
    while True:
        side = min(img.size) * scale
        frame = img if scale == 1.0 else img.resize(
            (round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
        for quality in (85, 75, 65, 55, 45):
            out = io.BytesIO()
            frame.save(out, "JPEG", quality=quality, optimize=True)
            if out.tell() <= max_bytes:
                return out.getvalue()
        if side * 0.85 < MIN_SIDE:
            raise FaceProvisioningError(f"nao deu para reduzir o JPEG de {len(jpeg)} bytes para {max_bytes} bytes")
        scale *= 0.85
