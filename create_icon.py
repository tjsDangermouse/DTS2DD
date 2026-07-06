"""Create application icon for DTS to AC-3 Converter."""
import os
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Error: Pillow not installed. Run: pip install pillow")
    exit(1)


def create_icon(output_path: str, size: int = 1024):
    """
    Create a simple application icon with DTS and AC-3 text.

    Args:
        output_path: Path to save the icon (should end in .png or .icns)
        size: Icon size in pixels
    """
    # Create a new image with a gradient-like background
    img = Image.new('RGB', (size, size), color='#2E3440')
    draw = ImageDraw.Draw(img)

    # Add a border
    border_color = '#3B4252'
    border_width = int(size * 0.05)
    draw.rectangle(
        [(border_width, border_width),
         (size - border_width, size - border_width)],
        outline=border_color,
        width=border_width
    )

    # Draw two circles for DTS and AC-3
    circle_radius = int(size * 0.2)
    margin = int(size * 0.15)

    # DTS circle (left)
    dts_x = margin + circle_radius
    dts_y = size // 2
    draw.ellipse(
        [(dts_x - circle_radius, dts_y - circle_radius),
         (dts_x + circle_radius, dts_y + circle_radius)],
        fill='#4C566A',
        outline='#81A1C1',
        width=3
    )

    # AC-3 circle (right)
    ac3_x = size - margin - circle_radius
    ac3_y = size // 2
    draw.ellipse(
        [(ac3_x - circle_radius, ac3_y - circle_radius),
         (ac3_x + circle_radius, ac3_y + circle_radius)],
        fill='#5E81AC',
        outline='#81A1C1',
        width=3
    )

    # Add arrow between them
    arrow_y = size // 2
    arrow_x1 = dts_x + circle_radius + int(size * 0.08)
    arrow_x2 = ac3_x - circle_radius - int(size * 0.08)

    draw.line(
        [(arrow_x1, arrow_y), (arrow_x2, arrow_y)],
        fill='#81A1C1',
        width=3
    )

    # Add arrow head
    arrow_head_size = int(size * 0.05)
    draw.polygon(
        [(arrow_x2, arrow_y),
         (arrow_x2 - arrow_head_size, arrow_y - arrow_head_size),
         (arrow_x2 - arrow_head_size, arrow_y + arrow_head_size)],
        fill='#81A1C1'
    )

    # Try to add text labels
    try:
        # Try using default font
        font_size = int(size * 0.12)
        font = ImageFont.load_default()

        # Draw "DTS" text
        dts_text = "DTS"
        draw.text(
            (dts_x - int(font_size * 0.4), dts_y - int(font_size * 0.3)),
            dts_text,
            fill='#ECEFF4',
            font=font
        )

        # Draw "AC3" text
        ac3_text = "AC3"
        draw.text(
            (ac3_x - int(font_size * 0.4), ac3_y - int(font_size * 0.3)),
            ac3_text,
            fill='#ECEFF4',
            font=font
        )
    except Exception:
        pass  # Font rendering is optional

    # Save as PNG
    img.save(output_path, 'PNG')
    print(f"✓ Icon created: {output_path}")


def convert_png_to_icns(png_path: str, icns_path: str):
    """
    Convert PNG to ICNS format for macOS.

    Args:
        png_path: Path to PNG file
        icns_path: Path to output ICNS file
    """
    try:
        import subprocess

        # Use sips command available on macOS
        result = subprocess.run(
            ['sips', '-s', 'format', 'icns', png_path, '--out', icns_path],
            capture_output=True
        )

        if result.returncode == 0:
            print(f"✓ ICNS icon created: {icns_path}")
            return True
        else:
            print(f"Note: Could not create ICNS format. PNG will be used instead.")
            return False
    except FileNotFoundError:
        print("Note: 'sips' command not found. PNG will be used for icon.")
        return False


if __name__ == "__main__":
    # Create icons directory
    icons_dir = Path("icons")
    icons_dir.mkdir(exist_ok=True)

    # Create PNG icon
    png_icon = str(icons_dir / "converter_icon.png")
    create_icon(png_icon, size=1024)

    # Try to convert to ICNS for macOS
    icns_icon = str(icons_dir / "converter_icon.icns")
    convert_png_to_icns(png_icon, icns_icon)

    # Create a smaller Windows-compatible ICO
    try:
        from PIL import Image
        png_img = Image.open(png_icon)

        # Create 256x256 version
        ico_sizes = [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
        ico_path = str(icons_dir / "converter_icon.ico")

        # ICO files typically use a single size or multiple sizes
        img_256 = png_img.resize((256, 256), Image.Resampling.LANCZOS)
        img_256.save(ico_path, 'ICO', sizes=[(256, 256)])
        print(f"✓ ICO icon created: {ico_path}")
    except Exception as e:
        print(f"Note: Could not create ICO format: {e}")

    print("\n✓ Icon generation complete!")
