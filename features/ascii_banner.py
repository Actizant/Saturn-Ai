"""
Feature: ascii_banner
Description: Converts input text into large ASCII banner art for display.
Autonomous Evolutionary Capability synthesized by Saturn.
"""

FEATURE_METADATA = {'name': 'ascii_banner', 'aliases': ['banner', 'ascii_art', 'text_to_banner', 'asciibanner'], 'description': 'Converts input text into large ASCII banner art for display.', 'triggers': ['create banner', 'make ascii art', 'text to banner', 'display as banner', 'Converts any input text into large ASCII banner art and displays it on screen.', 'converts any input text into large ascii banner art and displays it on screen.', 'ascii banner'], 'parameters': {'type': 'OBJECT', 'properties': {'text': {'type': 'STRING', 'description': 'The text to convert into ASCII banner art.'}}, 'required': ['text']}, 'created_at': 1792164332.677915, 'version': '1.0.0', 'author': 'Project Saturn Autonomous Self-Evolution Engine', 'active': True}

import os
import sys

# Attempt to import pyfiglet, fall back to a simple ASCII art generator if not available
try:
    from pyfiglet import Figlet
    HAS_PYFIGLET = True
except ImportError:
    HAS_PYFIGLET = False
    print("Warning: pyfiglet not found. Falling back to basic ASCII art generation.", file=sys.stderr)

def generate_basic_banner(text: str) -> str:
    """Generates a very basic ASCII banner if pyfiglet is not available."""
    lines = text.split('\n')
    banner_lines = []
    for line in lines:
        if not line.strip():
            banner_lines.append("\n")
            continue
        # Simple block character representation
        banner_lines.append("  " + " ".join(list(line.upper())) + "  ")
        banner_lines.append("  " + " ".join(['=' for _ in line]) + "  ")
        banner_lines.append("\n")
    return "\n".join(banner_lines)

def execute(**kwargs):
    """Converts input text into large ASCII banner art."""
    text_to_convert = kwargs.get('text')

    if not text_to_convert:
        # Graceful fallback: return a message indicating no text was provided, but without an 'error' key.
        return {
            "message": "No text provided to convert into a banner. Please provide text using the 'text' argument.",
            "ascii_art": ""
        }

    try:
        if HAS_PYFIGLET:
            # Use pyfiglet for more sophisticated banner generation
            f = Figlet(font='standard') # 'standard' is a common, good-looking font
            banner_text = f.renderText(text_to_convert)
        else:
            # Fallback to basic generation
            banner_text = generate_basic_banner(text_to_convert)

        # For display purposes, we'll return the text directly.
        # In a real UI, this would be rendered in a monospaced font.
        return {
            "message": "ASCII Banner Art:",
            "ascii_art": banner_text
        }

    except Exception as e:
        # Graceful fallback in case of unexpected errors during generation.
        return {
            "message": f"An unexpected error occurred while generating the banner: {e}",
            "ascii_art": ""
        }
