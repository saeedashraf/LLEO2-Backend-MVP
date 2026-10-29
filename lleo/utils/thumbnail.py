"""
Satellite Thumbnail Generator for LLEO
Generates preview thumbnails with satellite imagery
"""

import os
import math
import requests
import io
from PIL import Image, ImageDraw, ImageFont


def _extract_bounds(location: dict):
    """Return (lat_min, lat_max, lng_min, lng_max) from any location shape we store."""
    b = location.get("bounds")
    if isinstance(b, dict) and all(k in b for k in ("lat_min", "lat_max", "lng_min", "lng_max")):
        return float(b["lat_min"]), float(b["lat_max"]), float(b["lng_min"]), float(b["lng_max"])
    lat, lng = location.get("latitude"), location.get("longitude")
    if isinstance(lat, dict) and isinstance(lng, dict) and "min" in lat and "min" in lng:
        return float(lat["min"]), float(lat["max"]), float(lng["min"]), float(lng["max"])
    return None


def _zoom_for_bounds(lat_min, lat_max, lng_min, lng_max, width, height, max_zoom=15, min_zoom=3) -> int:
    """Largest Web-Mercator zoom at which the bbox fits in width x height px."""
    def _merc_y(lat):
        lat = max(-85.05, min(85.05, lat))
        return (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0
    dx = max(abs(lng_max - lng_min) / 360.0, 1e-9)
    dy = max(abs(_merc_y(lat_max) - _merc_y(lat_min)), 1e-9)
    for z in range(max_zoom, min_zoom - 1, -1):
        world = 256 * (2 ** z)
        if dx * world <= width * 0.85 and dy * world <= height * 0.85:
            return z
    return min_zoom


def create_satellite_thumbnail(
    location: dict,
    analysis_type: str,
    thumbnail_path: str,
    start_year: int = None,
    end_year: int = None
) -> bool:
    """
    Create satellite thumbnail with actual map imagery.

    Args:
        location: Location dict with 'latitude', 'longitude', 'name' (flattened format)
        analysis_type: Type of analysis (e.g., 'vegetation_index', 'water_quality')
        thumbnail_path: Path where to save the thumbnail
        start_year: Optional start year for analysis
        end_year: Optional end year for analysis

    Returns:
        True if successful, False otherwise
    """
    try:
        # Handle both flattened and nested location formats
        if isinstance(location.get('latitude'), dict):
            lat_center = location['latitude']['center']
            lon_center = location['longitude']['center']
        else:
            lat_center = location['latitude']
            lon_center = location['longitude']

        location_name = location.get('name', 'Unknown Location')

        tile_size = 256
        image_width = 400
        image_height = 300

        # Fit the analysed region (bbox) into the image instead of a fixed
        # zoom around the centre point, and centre on the bbox itself.
        bounds = _extract_bounds(location)
        if bounds:
            lat_min, lat_max, lng_min, lng_max = bounds
            lat_center = (lat_min + lat_max) / 2.0
            lon_center = (lng_min + lng_max) / 2.0
            zoom = _zoom_for_bounds(lat_min, lat_max, lng_min, lng_max, image_width, image_height)
        else:
            zoom = 13

        def _world_px(lat_deg, lon_deg, z):
            """Lat/lon → absolute Web-Mercator pixel coordinates at zoom z."""
            lat_deg = max(-85.05, min(85.05, lat_deg))
            n = 256.0 * (2 ** z)
            x = (lon_deg + 180.0) / 360.0 * n
            y = (1.0 - math.asinh(math.tan(math.radians(lat_deg))) / math.pi) / 2.0 * n
            return x, y

        cx, cy = _world_px(lat_center, lon_center, zoom)
        center_x_tile, center_y_tile = int(cx // tile_size), int(cy // tile_size)
        tiles_x = math.ceil(image_width / tile_size) + 2
        tiles_y = math.ceil(image_height / tile_size) + 2

        composite_width = tiles_x * tile_size
        composite_height = tiles_y * tile_size
        composite_img = Image.new('RGB', (composite_width, composite_height), (0, 0, 0))

        start_x = center_x_tile - tiles_x // 2
        start_y = center_y_tile - tiles_y // 2

        print(f"📍 Creating satellite thumbnail for {location_name} (zoom {zoom})")

        # Fetch map tiles
        for i in range(tiles_x):
            for j in range(tiles_y):
                tile_x = start_x + i
                tile_y = start_y + j

                try:
                    tile_url = f"https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{zoom}/{tile_y}/{tile_x}"
                    response = requests.get(tile_url, timeout=10)
                    response.raise_for_status()

                    tile_img = Image.open(io.BytesIO(response.content))
                    paste_x = i * tile_size
                    paste_y = j * tile_size
                    composite_img.paste(tile_img, (paste_x, paste_y))
                except Exception as e:
                    print(f"⚠️  Failed to get tile {tile_x},{tile_y}: {e}")
                    continue

        # Crop so the exact centre point sits in the middle of the image
        center_pixel_x = int(cx - start_x * tile_size)
        center_pixel_y = int(cy - start_y * tile_size)

        crop_left = center_pixel_x - image_width // 2
        crop_top = center_pixel_y - image_height // 2

        crop_left = max(0, min(crop_left, composite_width - image_width))
        crop_top = max(0, min(crop_top, composite_height - image_height))
        crop_right = crop_left + image_width
        crop_bottom = crop_top + image_height

        final_img = composite_img.crop((crop_left, crop_top, crop_right, crop_bottom))

        draw = ImageDraw.Draw(final_img)

        # Outline of the analysed region
        if bounds:
            x0, y0 = _world_px(lat_max, lng_min, zoom)
            x1, y1 = _world_px(lat_min, lng_max, zoom)
            ox, oy = start_x * tile_size + crop_left, start_y * tile_size + crop_top
            rx0, ry0, rx1, ry1 = x0 - ox, y0 - oy, x1 - ox, y1 - oy
            if rx1 - rx0 > 12 and ry1 - ry0 > 12:
                draw.rectangle([rx0 + 1, ry0 + 1, rx1 + 1, ry1 + 1], outline=(0, 0, 0), width=3)
                draw.rectangle([rx0, ry0, rx1, ry1], outline=(255, 220, 60), width=2)

        # Draw location pin
        pin_x = image_width // 2
        pin_y = image_height // 2
        pin_radius = 8

        # Pin shadow
        shadow_offset = 2
        draw.ellipse([
            pin_x - pin_radius + shadow_offset,
            pin_y - pin_radius + shadow_offset,
            pin_x + pin_radius + shadow_offset,
            pin_y + pin_radius + shadow_offset
        ], fill=(0, 0, 0, 100))

        # Pin body
        draw.ellipse([
            pin_x - pin_radius, pin_y - pin_radius,
            pin_x + pin_radius, pin_y + pin_radius
        ], fill=(220, 50, 50), outline=(255, 255, 255), width=2)

        # Pin center
        draw.ellipse([
            pin_x - 3, pin_y - 3,
            pin_x + 3, pin_y + 3
        ], fill=(255, 255, 255))

        # Load fonts
        try:
            font_title = ImageFont.truetype("arial.ttf", 16)
            font_small = ImageFont.truetype("arial.ttf", 11)
            font_coords = ImageFont.truetype("arial.ttf", 9)
        except:
            font_title = ImageFont.load_default()
            font_small = ImageFont.load_default()
            font_coords = ImageFont.load_default()

        # Create overlay for text
        overlay = Image.new('RGBA', final_img.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)

        # Prepare text
        analysis_type_text = analysis_type.replace('_', ' ').title()
        if 'vegetation' in analysis_type.lower() and start_year and end_year:
            analysis_type_text = f"NDVI Analysis {start_year}-{end_year}"

        coord_text = f"{lat_center:.4f}°N, {lon_center:.4f}°E"

        # Draw semi-transparent bars
        overlay_draw.rectangle([0, 0, image_width, 50], fill=(0, 0, 0, 160))
        overlay_draw.rectangle([0, image_height-30, image_width, image_height], fill=(0, 0, 0, 160))

        # Composite overlay
        final_img = Image.alpha_composite(final_img.convert('RGBA'), overlay).convert('RGB')

        # Draw text on final image
        draw = ImageDraw.Draw(final_img)
        draw.text((10, 8), location_name[:40], fill=(255, 255, 255), font=font_title)
        draw.text((10, 28), analysis_type_text, fill=(200, 200, 200), font=font_small)
        draw.text((10, image_height-22), coord_text, fill=(255, 255, 255), font=font_coords)

        # Analysis type color badge
        type_colors = {
            'vegetation_index': (100, 255, 100),
            'ndvi': (100, 255, 100),
            'air_quality': (255, 200, 100),
            'water_quality': (100, 200, 255),
            'business_location': (255, 100, 255),
            'weather_forecast': (255, 255, 100),
        }

        type_color = type_colors.get(analysis_type.lower(), (255, 255, 255))
        draw.text((image_width-80, 8), analysis_type.upper()[:8], fill=type_color, font=font_small)

        # Save thumbnail
        final_img.save(thumbnail_path, 'PNG', quality=95, optimize=True)

        print(f"✅ Satellite thumbnail created: {thumbnail_path}")
        return True

    except Exception as e:
        print(f"❌ Satellite thumbnail failed: {e}")
        import traceback
        traceback.print_exc()
        return create_fallback_thumbnail(analysis_type, location.get('name', 'Unknown'), thumbnail_path)


def create_fallback_thumbnail(analysis_type: str, location_name: str, thumbnail_path: str) -> bool:
    """Fallback thumbnail if satellite imagery fails."""
    try:
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(4, 3), facecolor='white')

        # Icons for different analysis types
        icons = {
            'vegetation_index': '🌿',
            'ndvi': '🌿',
            'air_quality': '🌫️',
            'water_quality': '💧',
            'business_location': '📍',
            'weather_forecast': '☀️',
        }

        icon = icons.get(analysis_type.lower(), '📊')
        title = analysis_type.replace('_', ' ').title()

        ax.text(0.5, 0.6, icon, ha='center', va='center',
               fontsize=48, fontweight='bold')
        ax.text(0.5, 0.3, title, ha='center', va='center',
               fontsize=14, fontweight='bold')
        ax.text(0.5, 0.1, location_name[:30], ha='center', va='center',
               fontsize=10, color='darkgray')

        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis('off')
        ax.set_facecolor('#f8f9fa')

        plt.tight_layout()
        plt.savefig(thumbnail_path, dpi=100, bbox_inches='tight',
                   facecolor='white', edgecolor='none')
        plt.close()

        print(f"✅ Fallback thumbnail created: {thumbnail_path}")
        return True

    except Exception as e:
        print(f"❌ All thumbnail methods failed: {e}")
        return False
