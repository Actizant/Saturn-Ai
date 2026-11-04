"""
actions/image_generator.py
Neural AI Image Generation Engine for Saturn.
Powered by Pollinations AI & FLUX / SDXL Neural Diffusion.

Generates high-resolution digital art, photorealistic portraits,
3D renders, and concept designs directly from natural language prompts.
Saves to Desktop/Saturn_Images, displays on the Circuit HUD, and opens automatically.
"""

import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from core.user_paths import get_user_data_dir

logger = logging.getLogger("ImageGenerator")
logger.setLevel(logging.INFO)

PLUGIN = {
    "name": "generate_image",
    "description": (
        "Generates stunning AI images, concept art, photorealistic photos, and 3D renders from text prompts. "
        "Saves the high-resolution image to Desktop, shows a preview on the HUD, and opens it automatically. "
        "Use whenever the user asks to generate, create, draw, paint, or visualize an image or picture."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "prompt": {
                "type": "STRING",
                "description": "Detailed text description of the image to generate."
            },
            "style": {
                "type": "STRING",
                "description": "Style: 'photorealistic', 'cyberpunk', 'anime', 'cinematic_3d', 'digital_art', 'concept_art', 'fantasy', or 'minimalist' (default: photorealistic)."
            },
            "aspect_ratio": {
                "type": "STRING",
                "description": "Aspect ratio: '1:1' (square), '16:9' (landscape), '9:16' (portrait) (default: 1:1)."
            },
            "destination": {
                "type": "STRING",
                "description": "Optional custom folder path to save the generated image."
            }
        },
        "required": ["prompt"]
    }
}

STYLE_PROMPT_ENHANCERS = {
    "photorealistic": "8k uhd, photorealistic, cinematic lighting, sharp focus, master photography, professional camera, highly detailed texture",
    "cyberpunk": "cyberpunk style, neon lights, volumetric smoke, futuristic city aesthetics, vibrant cyan and magenta glow, ultra detailed",
    "anime": "studio anime aesthetic, vibrant colors, makoto shinkai style, dynamic lighting, beautiful detailed composition",
    "cinematic_3d": "octane render, unreal engine 5, 3d render, dramatic cinematic illumination, ray tracing, volumetric fog, hyper-detailed",
    "digital_art": "artstation trending, digital painting, expressive brushwork, intricate details, vivid atmosphere",
    "concept_art": "sci-fi concept art, matte painting, epic scale, atmospheric depth, cinematic frame, award winning",
    "fantasy": "high fantasy, magical ethereal glow, mystical atmosphere, majestic composition, highly ornate details",
    "minimalist": "minimalist aesthetic, clean lines, elegant typography, modern graphic design, high contrast"
}


def _get_output_directory(custom_path: Optional[str] = None) -> Path:
    if custom_path:
        out = Path(custom_path)
    else:
        # Default: Desktop/Saturn_Images
        desktop = Path.home() / "Desktop" / "Saturn_Images"
        out = desktop
    out.mkdir(parents=True, exist_ok=True)
    return out


def _build_enhanced_prompt(prompt: str, style: str = "photorealistic") -> str:
    cleaned = re.sub(r"\s+", " ", prompt.strip())
    style_key = style.lower().strip().replace(" ", "_").replace("-", "_")
    enhancer = STYLE_PROMPT_ENHANCERS.get(style_key, STYLE_PROMPT_ENHANCERS["photorealistic"])
    
    # Avoid duplicating style terms if user already provided them
    if any(tok in cleaned.lower() for tok in ("photorealistic", "cyberpunk", "anime", "cinematic", "octane")):
        return cleaned
    return f"{cleaned}, {enhancer}"


def generate_image(
    parameters: Optional[Dict[str, Any]] = None,
    player: Any = None,
    speak: Any = None,
    **kwargs: Any
) -> str:
    """
    Main image synthesis entrypoint.
    Downloads the neural diffusion image from Pollinations and saves it locally.
    """
    params = dict(parameters or {})
    params.update(kwargs)

    raw_prompt = (params.get("prompt") or params.get("query") or params.get("description") or "").strip()
    if not raw_prompt:
        return "Error: Please provide a description of the image to generate."

    style = (params.get("style") or "photorealistic").lower().strip()
    aspect_ratio = params.get("aspect_ratio", "1:1")
    destination = params.get("destination")

    enhanced_prompt = _build_enhanced_prompt(raw_prompt, style)
    out_dir = _get_output_directory(destination)

    # Prepare file name
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    clean_slug = re.sub(r"[^a-zA-Z0-9_]", "_", raw_prompt[:25]).strip("_") or "artwork"
    image_file = out_dir / f"saturn_{clean_slug}_{timestamp}.jpg"

    if player and hasattr(player, "write_log"):
        player.write_log(f"🎨 [ImageGen] Synthesizing image: '{raw_prompt}' ({style})...")
    if speak:
        speak(f"Synthesizing your image, sir. Rendering with {style} neural diffusion.")

    # Encode URL
    encoded_prompt = urllib.parse.quote(enhanced_prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?nologo=true"

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
        )

        t0 = time.time()
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                raise ValueError(f"HTTP error {resp.status} from image generation service.")
            image_bytes = resp.read()

        elapsed = round(time.time() - t0, 1)

        # Write to disk
        with open(image_file, "wb") as f:
            f.write(image_bytes)

        kb_size = round(len(image_bytes) / 1024, 1)
        logger.info(f"[ImageGen] Saved {image_file} ({kb_size} KB in {elapsed}s)")

        # Render deliverable on Circuit HUD if available
        if player and hasattr(player, "show_hud_deliverable"):
            try:
                player.show_hud_deliverable(
                    title=f"AI Image: {raw_prompt[:25]}",
                    summary=f"Style: {style.title()}\nSize: {kb_size} KB\nSaved to: {image_file.name}",
                    file_path=str(image_file.resolve()),
                    kind="image"
                )
            except Exception:
                pass

        if player and hasattr(player, "write_log"):
            player.write_log(f"✅ [ImageGen] Image rendered successfully ({kb_size} KB in {elapsed}s).\nSaved to: {image_file}")

        # Automatically open in Windows Photos
        try:
            os.startfile(str(image_file.resolve()))
        except Exception:
            pass

        if speak:
            speak("Your image has been generated and saved to your desktop, sir.")

        return (
            f"✅ **Image Generated Successfully**\n\n"
            f"- **Prompt**: {raw_prompt}\n"
            f"- **Style**: {style.title()}\n"
            f"- **File Path**: `{image_file}`\n"
            f"- **Resolution**: High-Res ({kb_size} KB)\n"
            f"- **Render Time**: {elapsed}s\n\n"
            f"The image has been opened on your screen!"
        )

    except Exception as exc:
        err_msg = f"Failed to generate image: {exc}"
        logger.error(f"[ImageGen] {err_msg}")
        if player and hasattr(player, "write_log"):
            player.write_log(f"ERR: [ImageGen] {err_msg}")
        if speak:
            speak("There was an issue rendering the image, sir. Please check the logs.")
        return f"Error: {err_msg}"
