#!/usr/bin/env python3
"""Generate images with the APIQIK OpenAI-compatible image API."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import boto3
    from botocore.config import Config
except ImportError:
    boto3 = None
    Config = None


DEFAULT_BASE_URL = "https://value.apiqik.online"
DEFAULT_MODEL = "gpt-image-2-flatfee-4k"
DEFAULT_MODEL_SEQUENCE = [
    "gpt-image-2-flatfee-4k",
    "gpt-image-2-flatfee-2k",
    "gpt-image-2-vip",
]
SUPPORTED_RATIOS = {
    "1:1",
    "2:3",
    "3:2",
    "3:4",
    "4:3",
    "4:5",
    "5:4",
    "9:16",
    "16:9",
    "21:9",
}
SUPPORTED_SIZES = {
    "1024x1024",
    "1536x1024",
    "1024x1536",
    "2048x2048",
    "2048x1152",
    "3840x2160",
    "2160x3840",
}
SUPPORTED_QUALITIES = {"high"}
DEFAULT_UPLOAD_CACHE_PATH = Path("data/image_editor_r2_upload_cache.json")
DEFAULT_PHASH_DISTANCE = 0
_UPLOAD_CACHE_LOCK = threading.RLock()


def is_http_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def load_env_value(name: str, env_path: Path = Path(".env")) -> str | None:
    """Read one value from the process environment or a simple .env file."""
    if os.getenv(name):
        return os.environ[name]

    if not env_path.exists():
        return None

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        if key.strip() != name:
            continue

        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value

    return None





def build_payload_chat(
    *,
    prompt: str,
    model: str,
    image_urls: list[str],
    n: int = 1,
    size: str | None = "1024x1024",
    group: str = "codex-image",
) -> dict[str, Any]:
    """Build the /v1/chat/completions request body."""
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt.strip()}]
    for url in image_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})

    payload: dict[str, Any] = {
        "model": model,
        "group": group,
        "messages": [{"role": "user", "content": content}],
        "image_config": {"n": n},
        "stream": False,
        "temperature": 0.7,
        "top_p": 1,
    }
    if size:
        payload["image_config"]["size"] = size
    return payload


def extract_image_urls(text: str) -> list[str]:
    """Extract image URLs from Markdown or plain text."""
    import re

    urls = re.findall(r"!\[.*?\]\((https?://.*?)\)", text)
    if urls:
        return urls

    return re.findall(
        r"(https?://[^\s\)\>]+(?:\.png|\.jpg|\.jpeg|\.webp))",
        text,
        re.IGNORECASE,
    )


def extract_url_from_markdown(text: str) -> str | None:
    """Extract the first image URL from Markdown like ![alt](url)."""
    urls = extract_image_urls(text)
    return urls[0] if urls else None


def generate_image(
    *,
    api_key: str,
    prompt: str,
    model: str = DEFAULT_MODEL,
    n: int = 1,
    size: str | None = "1024x1024",
    ratio: str | None = "1:1",
    quality: str = "high",
    image_urls: list[str] | None = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: int = 600,
    group: str = "codex-image",
) -> dict[str, Any]:
    """Call the image generation endpoint via Chat Completions."""
    payload = build_payload_chat(
        prompt=prompt,
        model=model,
        image_urls=image_urls or [],
        n=n,
        size=size,
        group=group
    )
    endpoint = f"{base_url.rstrip('/')}/v1/chat/completions"

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API request failed with HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise RuntimeError(f"API request failed: {error.reason}") from error


def file_sha256(path: Path) -> str:
    """Return SHA-256 digest for exact local-file cache matching."""
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def perceptual_hash(image_path: Path, *, hash_size: int = 8, highfreq_factor: int = 4) -> str:
    """Compute a DCT-based perceptual hash (pHash) for a local image.

    The implementation intentionally avoids the external ``imagehash`` package.
    It requires Pillow, which is already available in the Mabobot venv.
    """
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required for perceptual image hashing") from exc

    img_size = hash_size * highfreq_factor
    with Image.open(image_path) as image:
        image = image.convert("L").resize((img_size, img_size), Image.Resampling.LANCZOS)
        pixels = [list(map(float, image.crop((0, y, img_size, y + 1)).getdata())) for y in range(img_size)]

    def alpha(index: int) -> float:
        return math.sqrt(1 / img_size) if index == 0 else math.sqrt(2 / img_size)

    dct_low: list[float] = []
    for u in range(hash_size):
        for v in range(hash_size):
            total = 0.0
            for x in range(img_size):
                cos_x = math.cos(((2 * x + 1) * u * math.pi) / (2 * img_size))
                for y in range(img_size):
                    cos_y = math.cos(((2 * y + 1) * v * math.pi) / (2 * img_size))
                    total += pixels[x][y] * cos_x * cos_y
            dct_low.append(alpha(u) * alpha(v) * total)

    values = dct_low[1:]  # ignore the DC coefficient
    sorted_values = sorted(values)
    median = sorted_values[len(sorted_values) // 2]
    bits = 0
    for value in dct_low:
        bits = (bits << 1) | int(value > median)
    return f"{bits:0{hash_size * hash_size // 4}x}"


def hamming_distance(hex_a: str, hex_b: str) -> int:
    """Return Hamming distance between two hexadecimal perceptual hashes."""
    return (int(hex_a, 16) ^ int(hex_b, 16)).bit_count()


def _load_upload_cache(cache_path: Path) -> dict[str, Any]:
    if not cache_path.exists():
        return {"version": 1, "entries": []}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            return data
    except Exception:
        pass
    return {"version": 1, "entries": []}


def _save_upload_cache(cache_path: Path, cache: dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(cache_path)


def _find_cached_image_url(
    cache: dict[str, Any],
    *,
    sha256: str,
    phash: str,
    phash_distance: int,
) -> str | None:
    entries = cache.get("entries") or []
    for entry in entries:
        if entry.get("sha256") == sha256 and entry.get("url"):
            return entry["url"]

    for entry in entries:
        cached_phash = entry.get("phash")
        if not cached_phash or not entry.get("url"):
            continue
        try:
            if hamming_distance(phash, cached_phash) <= phash_distance:
                return entry["url"]
        except Exception:
            continue
    return None


def resolve_cached_or_upload_image(
    image_path: Path,
    *,
    r2_config: dict[str, str | None],
    cache_path: Path = DEFAULT_UPLOAD_CACHE_PATH,
    phash_distance: int = DEFAULT_PHASH_DISTANCE,
    timeout: int = 300,
) -> str:
    """Resolve a local image to an R2 public URL, reusing local pHash cache when possible."""
    sha256_value = file_sha256(image_path)
    phash_value = perceptual_hash(image_path)

    # Keep the lock across upload to avoid duplicate uploads for concurrent identical inputs.
    with _UPLOAD_CACHE_LOCK:
        cache = _load_upload_cache(cache_path)
        cached_url = _find_cached_image_url(
            cache,
            sha256=sha256_value,
            phash=phash_value,
            phash_distance=phash_distance,
        )
        if cached_url:
            return cached_url

        missing = [key for key, value in r2_config.items() if not value]
        if missing:
            raise ValueError(f"Missing R2 configuration in .env: {', '.join(missing)}")

        uploaded_url = upload_image_to_r2(
            image_path,
            **r2_config,
            timeout=timeout,
        )
        entries = cache.setdefault("entries", [])
        entries.append(
            {
                "phash": phash_value,
                "sha256": sha256_value,
                "url": uploaded_url,
                "file_name": image_path.name,
                "file_size": image_path.stat().st_size,
                "uploaded_at": int(time.time()),
            }
        )
        _save_upload_cache(cache_path, cache)
        return uploaded_url


def upload_image_to_r2(
    image_path: Path,
    *,
    access_key: str,
    secret_key: str,
    account_id: str,
    bucket_name: str,
    public_url_prefix: str,
    timeout: int = 300,
) -> str:
    """Upload a local image file to Cloudflare R2 and return the public image URL."""
    if boto3 is None:
        raise RuntimeError("boto3 is not installed. Please run 'pip install boto3'")

    if not image_path.is_file():
        raise ValueError(f"Reference image file not found: {image_path}")

    s3_client = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )

    # 创建唯一文件名
    timestamp = int(time.time() * 1000)
    file_key = f"apiqik_uploads/{timestamp}_{image_path.name}"

    # 简单的 ContentType 映射
    content_type = "image/png"
    if image_path.suffix.lower() in [".jpg", ".jpeg"]:
        content_type = "image/jpeg"
    elif image_path.suffix.lower() == ".webp":
        content_type = "image/webp"

    try:
        s3_client.upload_file(
            str(image_path),
            bucket_name,
            file_key,
            ExtraArgs={"ContentType": content_type}
        )
    except Exception as e:
        raise RuntimeError(f"Cloudflare R2 upload failed: {e}")

    return f"{public_url_prefix.rstrip('/')}/{file_key}"


def resolve_image_inputs(
    image_inputs: list[str],
    *,
    env_path: Path = Path(".env"),
    timeout: int = 300,
    upload_cache_path: Path = DEFAULT_UPLOAD_CACHE_PATH,
    phash_distance: int = DEFAULT_PHASH_DISTANCE,
) -> list[str]:
    """Convert URL/local reference image inputs into URLs accepted by APIQIK."""
    image_urls: list[str] = []

    # 预加载 R2 配置
    r2_config = {
        "access_key": load_env_value("CF_ACCESS_KEY", env_path),
        "secret_key": load_env_value("CF_SECRET_KEY", env_path),
        "account_id": load_env_value("CF_ACCOUNT_ID", env_path),
        "bucket_name": load_env_value("CF_BUCKET", env_path),
        "public_url_prefix": load_env_value("CF_PUBLIC_URL", env_path),
    }

    for image_input in image_inputs:
        if is_http_url(image_input):
            image_urls.append(image_input)
            continue

        image_path = Path(image_input).expanduser()
        if not image_path.is_file():
            raise ValueError(f"Reference image is not a URL or local file: {image_input}")

        image_urls.append(
            resolve_cached_or_upload_image(
                image_path,
                r2_config=r2_config,
                cache_path=upload_cache_path,
                phash_distance=phash_distance,
                timeout=timeout,
            )
        )
    return image_urls


def save_generation_result(response: dict[str, Any], output_path: Path) -> list[Path]:
    """Save ALL generated images from Chat response."""
    saved_paths = []

    # Chat Completions response
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        all_urls = []
        for choice in choices:
            content = choice.get("message", {}).get("content", "")
            all_urls.extend(extract_image_urls(content))

        for i, url in enumerate(all_urls):
            current_path = _get_indexed_path(output_path, i + 1 if len(all_urls) > 1 else 0)
            saved_paths.append(_download_and_save(url, current_path))

    if not saved_paths:
        raise ValueError(f"No images found in response: {json.dumps(response)[:200]}...")

    return saved_paths


def _get_indexed_path(base_path: Path, index: int) -> Path:
    """Helper to add an index suffix to a filename if needed."""
    if index <= 0:
        return base_path
    return base_path.with_name(f"{base_path.stem}_{index}{base_path.suffix}")


def _download_and_save(url: str, output_path: Path) -> Path:
    """Helper to download a URL and save to path."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": "apiqik-image-client/1.0"})
    with urlopen(request, timeout=300) as response_obj:
        output_path.write_bytes(response_obj.read())
    return output_path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate an image with APIQIK's OpenAI-compatible API."
    )
    parser.add_argument("prompt", nargs="?", help="Image prompt")
    parser.add_argument("--prompt", dest="prompt_option", help="Image prompt")
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        help="Reference image URL or local file path",
    )
    parser.add_argument(
        "--image-url",
        action="append",
        default=[],
        help="Reference image URL or local file path, same as --image",
    )
    parser.add_argument("--output", "-o", default="generated_apiqik.png")
    parser.add_argument("--model", default=os.getenv("APIQIK_IMAGE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--base-url", default=os.getenv("APIQIK_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--size", default="1024x1024", choices=sorted(SUPPORTED_SIZES))
    parser.add_argument("--ratio", default="1:1", choices=sorted(SUPPORTED_RATIOS))
    parser.add_argument("--quality", default="high", choices=sorted(SUPPORTED_QUALITIES))
    parser.add_argument("--n", type=int, default=1, help="Number of images to generate")
    parser.add_argument("--timeout", type=int, default=120)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    prompt = args.prompt_option or args.prompt
    if not prompt:
        print("Missing prompt. Example: python apiqik_image2.py \"一只猫在月球上\"", file=sys.stderr)
        return 2

    api_key = load_env_value("APIQIK_KEY", Path(args.env_file))
    if not api_key:
        print("Missing APIQIK_KEY in environment or .env", file=sys.stderr)
        return 2

    try:
        image_urls = resolve_image_inputs(
            args.image + args.image_url,
            env_path=Path(args.env_file),
            timeout=args.timeout,
        )
        response = generate_image(
            api_key=api_key,
            prompt=prompt,
            model=args.model,
            n=args.n,
            size=args.size,
            ratio=args.ratio,
            quality=args.quality,
            image_urls=image_urls,
            base_url=args.base_url,
            timeout=args.timeout,
        )
        output_path = save_generation_result(response, Path(args.output))
    except (RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1

    data = response.get("data") or []
    if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("url"):
        print(f"Image URL: {data[0]['url']}")
    print(f"Saved image: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
