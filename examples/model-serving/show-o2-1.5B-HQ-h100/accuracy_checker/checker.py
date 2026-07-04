"""HTTP smoke checker for a Show-o2 image generation server."""

from __future__ import annotations

import argparse
import base64
import sys

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description="Check a Show-o2 HTTP server returns a PNG image.")
    parser.add_argument("--url", default="http://localhost:8000", help="Server base URL")
    parser.add_argument("--endpoint", default="/v1/images/generations", help="Endpoint path")
    parser.add_argument("--prompt", default="a readable sign that says VibeServe", help="Prompt")
    parser.add_argument("--steps", type=int, default=4, help="Diffusion inference steps")
    parser.add_argument("--guidance-scale", type=float, default=5.0, help="Guidance scale")
    parser.add_argument("--timeout", type=float, default=600.0, help="Request timeout")
    args = parser.parse_args()

    url = args.url.rstrip("/") + args.endpoint
    body = {
        "prompt": args.prompt,
        "num_inference_steps": args.steps,
        "guidance_scale": args.guidance_scale,
    }
    try:
        response = httpx.post(url, json=body, timeout=args.timeout)
        response.raise_for_status()
        payload = response.json()
        image_bytes = base64.b64decode(payload["data"][0]["b64_json"])
        if not image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("response image is not a PNG")
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(f"PASS: received {len(image_bytes)} PNG bytes from {url}")


if __name__ == "__main__":
    main()
