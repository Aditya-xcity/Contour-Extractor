"""Local Flask tracing workspace for the contour extractor."""

from __future__ import annotations

import base64
import binascii
import io
import secrets
import threading
import time
from pathlib import Path
from uuid import uuid4

from flask import Flask, abort, jsonify, render_template, request, send_file
from PIL import Image, UnidentifiedImageError
from werkzeug.utils import secure_filename

from contour import _DETAIL_PRESETS, extract_contour

ROOT = Path(__file__).resolve().parent
UPLOAD_DIR = ROOT / "uploads"
OUTPUT_DIR = ROOT / "outputs"
EXPORT_DIR = ROOT / "exports"
for directory in (UPLOAD_DIR, OUTPUT_DIR, EXPORT_DIR):
    directory.mkdir(exist_ok=True)

ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "BMP", "TIFF"}
MAX_UPLOAD_BYTES = 16 * 1024 * 1024
MAX_PIXELS = 25_000_000
FILE_TTL_SECONDS = 24 * 60 * 60
PROCESS_LOCK = threading.Lock()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


def _cleanup_files() -> None:
    cutoff = time.time() - FILE_TTL_SECONDS
    for directory in (UPLOAD_DIR, OUTPUT_DIR, EXPORT_DIR):
        for path in directory.iterdir():
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)


def _safe_id(value: str) -> Path:
    if not value or Path(value).name != value or Path(value).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}:
        abort(404)
    path = (UPLOAD_DIR / value).resolve()
    if path.parent != UPLOAD_DIR.resolve():
        abort(404)
    return path


def _validate_image(stream: io.BufferedIOBase) -> Image.Image:
    try:
        image = Image.open(stream)
        image.verify()
        stream.seek(0)
        image = Image.open(stream)
        if image.format not in ALLOWED_FORMATS:
            raise ValueError("That image format is not supported.")
        if image.width * image.height > MAX_PIXELS:
            raise ValueError("The image is too large. Please use an image under 25 megapixels.")
        return image.copy()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("The uploaded file is not a valid image.") from exc


def _output_path(image_id: str) -> Path:
    if not image_id or Path(image_id).name != image_id or Path(image_id).suffix.lower() != ".png":
        abort(404)
    path = (OUTPUT_DIR / image_id).resolve()
    if path.parent != OUTPUT_DIR.resolve():
        abort(404)
    return path


def _number(data: dict, key: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(data.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number.") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}.")
    return value


def _settings(data: dict) -> dict:
    style = data.get("style", "lineart")
    if style not in {"lineart", "lineart-anime", "canny", "sketch"}:
        raise ValueError("Unknown processing style.")
    background = data.get("background", "transparent")
    if background not in {"transparent", "white"}:
        raise ValueError("Unknown background.")
    detail = data.get("detail_level") or None
    if detail not in {None, *_DETAIL_PRESETS}:
        raise ValueError("Unknown detail level.")
    # Apply the same preset values as the CLI first, then apply explicit form
    # values. This makes a preset a baseline rather than a silent reset.
    settings = dict(_DETAIL_PRESETS.get(detail, {}))
    settings.update({
        "style": style,
        "background": background,
        "outer_thickness": _number(data, "outer_thickness", 1, 1, 12),
        "inner_thickness": _number(data, "inner_thickness", 1, 0, 12),
        "lineart_resolution": _number(data, "lineart_resolution", 512, 256, 768),
        "min_component_size": _number(
            data, "min_component_size", settings.get("min_component_size", 4), 0, 10000
        ),
        "flatten": _number(data, "flatten", 0, 0, 100),
        "segment_foreground": data.get("segment_foreground", True) is not False,
    })
    settings["detail_level"] = detail
    return settings


@app.errorhandler(413)
def too_large(_error):
    return jsonify(error="The upload is too large (maximum 16 MB)."), 413


@app.get("/")
def index():
    _cleanup_files()
    return render_template("index.html")


@app.post("/upload")
def upload():
    _cleanup_files()
    file = request.files.get("image")
    if file is None or not file.filename:
        return jsonify(error="Choose an image to upload."), 400
    try:
        image = _validate_image(file.stream)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    image_id = f"{uuid4().hex}{Path(secure_filename(file.filename)).suffix.lower() or '.png'}"
    image.save(UPLOAD_DIR / image_id)
    return jsonify(image_id=image_id, preview_url=f"/image/{image_id}")


@app.post("/generate-outline")
def generate_outline():
    _cleanup_files()
    payload_json = request.get_json(silent=True) or {}
    image_id = request.form.get("image_id") or payload_json.get("image_id")
    input_path = _safe_id(image_id)
    if not input_path.is_file():
        return jsonify(error="The uploaded image has expired. Please upload it again."), 404
    payload = request.form.to_dict() if request.form else payload_json
    try:
        settings = _settings(payload)
        output_id = f"{uuid4().hex}.png"
        output_path = OUTPUT_DIR / output_id
        with PROCESS_LOCK:
            extract_contour(
                input_path,
                output_path,
                style=settings["style"],
                background=settings["background"],
                outer_thickness=settings["outer_thickness"],
                inner_thickness=0 if settings.get("no_inner") else settings["inner_thickness"],
                lineart_resolution=settings["lineart_resolution"],
                min_component_size=settings["min_component_size"],
                flatten=settings["flatten"],
                model=payload.get("model", "u2net"),
                segment_foreground=settings["segment_foreground"],
            )
    except (ValueError, RuntimeError) as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        app.logger.exception("Outline generation failed")
        return jsonify(error="Outline generation failed. Check the image and selected model."), 500
    return jsonify(image_id=output_id, result_url=f"/result/{output_id}", trace_url=f"/trace/{output_id}")


@app.get("/result/<image_id>")
def result(image_id: str):
    path = _output_path(image_id)
    if not path.is_file():
        abort(404)
    return render_template("result.html", image_id=image_id)


@app.get("/image/<image_id>")
def image(image_id: str):
    input_path = _safe_id(image_id)
    if input_path.is_file():
        return send_file(input_path)
    output_path = _output_path(image_id)
    if output_path.is_file():
        return send_file(output_path)
    abort(404)


@app.get("/trace/<image_id>")
def trace(image_id: str):
    path = _output_path(image_id)
    if not path.is_file():
        abort(404)
    return render_template("trace.html", image_id=image_id)


@app.post("/export")
def export():
    payload = request.get_json(silent=True) or {}
    data_url = payload.get("image")
    if not isinstance(data_url, str) or not data_url.startswith("data:image/png;base64,"):
        return jsonify(error="A PNG drawing is required."), 400
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Export must be a PNG.")
        image = _validate_image(io.BytesIO(raw))
        if payload.get("background") == "white":
            rgba = image.convert("RGBA")
            white = Image.new("RGBA", rgba.size, "white")
            white.alpha_composite(rgba)
            image = white
        export_id = f"{secrets.token_hex(16)}.png"
        path = EXPORT_DIR / export_id
        image.save(path, format="PNG")
    except (binascii.Error, ValueError, UnidentifiedImageError, OSError) as exc:
        return jsonify(error=str(exc) or "The drawing is not a valid PNG."), 400
    return jsonify(download_url=f"/download/{export_id}")


@app.get("/download/<export_id>")
def download(export_id: str):
    if Path(export_id).name != export_id or Path(export_id).suffix.lower() != ".png":
        abort(404)
    path = (EXPORT_DIR / export_id).resolve()
    if path.parent != EXPORT_DIR.resolve() or not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name="tracing.png", mimetype="image/png")


if __name__ == "__main__":
    app.run(debug=True)
