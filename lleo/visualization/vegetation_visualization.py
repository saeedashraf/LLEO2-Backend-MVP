"""
Vegetation Index Visualization Module

Generates interactive maps and charts for Vegetation Index (NDVI) analysis
Extracted from main.py and adapted for LLEO package
"""

import folium
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg')  # Use non-interactive backend
import pandas as pd
import numpy as np
import ee
from typing import Dict, Any, Optional


# ── NDVI visual configuration ───────────────────────────────────────────────

NDVI_VIS = {
    'min': -0.2, 'max': 1.0,
    'palette': ['#8B0000', '#FF4500', '#FFD700', '#ADFF2F', '#228B22', '#006400'],
    'label': 'NDVI', 'unit': 'index',
}

NDVI_DIFF_VIS = {
    'min': -0.3, 'max': 0.3,
    'palette': ['#8B0000', '#FF6347', '#FFFACD', '#90EE90', '#006400'],
    'label': 'ΔNDVI', 'unit': 'change',
}


def _add_vertical_legend(m: folium.Map, vis: dict, pol_idx: int = 0) -> None:
    """Inject a compact vertical colour-scale legend into a Folium map."""
    n_stops = len(vis['palette'])
    vmin, vmax = vis['min'], vis['max']

    stops = ", ".join(
        f"{vis['palette'][i]} {int(i / (n_stops - 1) * 100)}%"
        for i in range(n_stops)
    )

    left_px = 12 + pol_idx * 58

    def _fmt(v: float) -> str:
        if abs(v) >= 100:
            return f"{v:.0f}"
        if abs(v) >= 1:
            return f"{v:.2f}"
        return f"{v:.2f}"

    html = f"""
    <div style="
        position: fixed; bottom: 30px; left: {left_px}px; z-index: 1000;
        background: rgba(255,255,255,0.92); border-radius: 6px;
        padding: 6px 8px; box-shadow: 0 2px 6px rgba(0,0,0,0.3);
        font-family: Arial, sans-serif; width: 46px;
    ">
        <div style="font-size: 11px; font-weight: 700; text-align: center;
                    margin-bottom: 4px; line-height: 1.1;">
            {vis['label']}
        </div>
        <div style="font-size: 9px; text-align: right; margin-bottom: 1px;
                    color: #333; font-weight: 600;">
            {_fmt(vmax)}
        </div>
        <div style="
            width: 22px; height: 120px; margin: 0 auto;
            background: linear-gradient(to bottom, {stops});
            border: 1px solid #999; border-radius: 2px;
        "></div>
        <div style="font-size: 9px; text-align: right; margin-top: 1px;
                    color: #333; font-weight: 600;">
            {_fmt(vmin)}
        </div>
        <div style="font-size: 8px; text-align: center; margin-top: 3px;
                    color: #666; font-weight: 600;">
            {vis['unit']}
        </div>
    </div>
    """
    m.get_root().html.add_child(folium.Element(html))


class VegetationVisualization:
    """Handles visualization generation for Vegetation Index analysis"""

    # NDVI classification scheme
    NDVI_CLASSES = {
        'labels': [
            'Bare Soil / Water (<0.2)',
            'Sparse Vegetation (0.2-0.4)',
            'Moderate Vegetation (0.4-0.6)',
            'Dense Vegetation (>0.6)'
        ],
        'colors': ['#8B4513', '#FFD700', '#ADFF2F', '#006400']
    }

    def __init__(self, start_year: int, end_year: int, location_name: str, bounds: Dict[str, Any]):
        """
        Initialize visualization generator

        Args:
            start_year: Analysis start year
            end_year: Analysis end year
            location_name: Name of analyzed location
            bounds: Dictionary with 'center', 'southwest', 'northeast' coordinates
        """
        self.start_year = start_year
        self.end_year = end_year
        self.location_name = location_name
        self.bounds = bounds

    def create_interactive_map(
        self,
        mean_ndvi_start: ee.Image,
        mean_ndvi_end: ee.Image,
        ndvi_difference: ee.Image,
        save_path: str = 'vegetation_index_map.html'
    ) -> None:
        """
        Create interactive Folium map with Earth Engine NDVI layers

        Args:
            mean_ndvi_start: Earth Engine Image - mean NDVI for start year
            mean_ndvi_end: Earth Engine Image - mean NDVI for end year
            ndvi_difference: Earth Engine Image - NDVI change
            save_path: Path to save HTML map file
        """
        print("🗺️  Creating interactive map with Vegetation Index layers...")

        # Create base map
        m = folium.Map(location=self.bounds['center'], zoom_start=12)

        # Add base layers
        folium.TileLayer('OpenStreetMap').add_to(m)
        folium.TileLayer(
            tiles='https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
            attr='© Esri',
            name='Satellite',
            overlay=False,
            control=True
        ).add_to(m)

        # Use module-level vis config
        ndvi_vis = {
            'min': NDVI_VIS['min'],
            'max': NDVI_VIS['max'],
            'palette': NDVI_VIS['palette'],
        }

        diff_vis = {
            'min': NDVI_DIFF_VIS['min'],
            'max': NDVI_DIFF_VIS['max'],
            'bands': ['NDVI'],
            'palette': NDVI_DIFF_VIS['palette'],
        }

        # Add Earth Engine layers
        try:
            # Add NDVI start year layer
            map_id_dict = ee.Image(mean_ndvi_start).getMapId(ndvi_vis)
            folium.raster_layers.TileLayer(
                tiles=map_id_dict['tile_fetcher'].url_format,
                attr='Map Data © Google Earth Engine',
                name=f'Vegetation Index {self.start_year}',
                overlay=True,
                control=True
            ).add_to(m)
            print(f"   ✅ Added Vegetation Index {self.start_year} layer")

            # Add NDVI end year layer
            map_id_dict = ee.Image(mean_ndvi_end).getMapId(ndvi_vis)
            folium.raster_layers.TileLayer(
                tiles=map_id_dict['tile_fetcher'].url_format,
                attr='Map Data © Google Earth Engine',
                name=f'Vegetation Index {self.end_year}',
                overlay=True,
                control=True
            ).add_to(m)
            print(f"   ✅ Added Vegetation Index {self.end_year} layer")

            # Add NDVI difference layer
            map_id_dict = ee.Image(ndvi_difference).getMapId(diff_vis)
            folium.raster_layers.TileLayer(
                tiles=map_id_dict['tile_fetcher'].url_format,
                attr='Map Data © Google Earth Engine',
                name=f'Vegetation Index Change ({self.start_year}-{self.end_year})',
                overlay=True,
                control=True
            ).add_to(m)
            print(f"   ✅ Added Vegetation Index Change layer")

        except Exception as e:
            print(f"   ⚠️  Could not add Earth Engine layers: {e}")

        # Add area boundary
        folium.Rectangle(
            bounds=[self.bounds['southwest'], self.bounds['northeast']],
            color='red',
            weight=2,
            fill=False,
            dash_array='5, 5'
        ).add_to(m)

        # Add marker with analysis info
        folium.Marker(
            self.bounds['center'],
            popup=f"<b>Vegetation Index Analysis Area</b><br>"
                  f"Location: {self.location_name}<br>"
                  f"Coordinates: {self.bounds['southwest'][0]:.4f}, {self.bounds['southwest'][1]:.4f} to "
                  f"{self.bounds['northeast'][0]:.4f}, {self.bounds['northeast'][1]:.4f}<br>"
                  f"Time Period: {self.start_year} to {self.end_year}",
            icon=folium.Icon(color='green', icon='info-sign')
        ).add_to(m)

        # Add layer control
        m.add_child(folium.LayerControl())

        # Add vertical colour-scale legends
        _add_vertical_legend(m, NDVI_VIS, pol_idx=0)
        _add_vertical_legend(m, NDVI_DIFF_VIS, pol_idx=1)

        # Save map
        m.save(save_path)
        print(f"✅ Interactive map saved to {save_path}")

    def create_charts(
        self,
        statistics: Dict[str, Any],
        figsize: tuple = (15, 10),
        save_path: str = 'vegetation_index_charts.png'
    ) -> None:
        """
        Create analysis charts with NDVI distribution and changes

        Args:
            statistics: Dictionary with 'start_year' and 'end_year' statistics
            figsize: Figure size (width, height)
            save_path: Path to save PNG file
        """
        print("📊 Creating analysis charts...")

        # Convert statistics to DataFrames
        df_start = pd.DataFrame(statistics['start_year']).set_index('label')
        df_end = pd.DataFrame(statistics['end_year']).set_index('label')

        df_comparison = pd.DataFrame({
            str(self.start_year): df_start['value'],
            str(self.end_year): df_end['value'],
            'Change (%)': df_end['value'] - df_start['value']
        })
        df_comparison['color'] = df_start['color']

        # Create figure with 4 subplots
        fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=figsize)
        fig.suptitle(
            f'Vegetation Index Analysis: {self.location_name} ({self.start_year} vs {self.end_year})',
            fontsize=16,
            fontweight='bold'
        )

        # 1. Pie chart for start year
        ax1.pie(
            df_start['value'],
            labels=df_start.index,
            autopct='%1.1f%%',
            startangle=90,
            colors=df_start['color']
        )
        ax1.set_title(f'Vegetation Distribution {self.start_year}', fontsize=12, fontweight='bold')

        # 2. Pie chart for end year
        ax2.pie(
            df_end['value'],
            labels=df_end.index,
            autopct='%1.1f%%',
            startangle=90,
            colors=df_end['color']
        )
        ax2.set_title(f'Vegetation Distribution {self.end_year}', fontsize=12, fontweight='bold')

        # 3. Bar chart comparison
        x_pos = np.arange(len(df_comparison))
        width = 0.35

        bars1 = ax3.bar(
            x_pos - width/2,
            df_comparison[str(self.start_year)],
            width,
            label=str(self.start_year),
            color=df_comparison['color'],
            alpha=0.7
        )
        bars2 = ax3.bar(
            x_pos + width/2,
            df_comparison[str(self.end_year)],
            width,
            label=str(self.end_year),
            color=df_comparison['color']
        )

        ax3.set_xlabel('Vegetation Index Categories', fontweight='bold')
        ax3.set_ylabel('Percentage (%)', fontweight='bold')
        ax3.set_title('Category Comparison', fontsize=12, fontweight='bold')
        ax3.set_xticks(x_pos)
        ax3.set_xticklabels([label.split(' (')[0] for label in df_comparison.index], rotation=45)
        ax3.legend()
        ax3.grid(True, alpha=0.3)

        # Add value labels on bars
        for bars in [bars1, bars2]:
            for bar in bars:
                height = bar.get_height()
                ax3.text(
                    bar.get_x() + bar.get_width()/2.,
                    height + 0.5,
                    f'{height:.1f}%',
                    ha='center',
                    va='bottom',
                    fontsize=8
                )

        # 4. Change bar chart
        change_bars = ax4.bar(
            df_comparison.index,
            df_comparison['Change (%)'],
            color=df_comparison['color']
        )
        ax4.axhline(0, color='black', linewidth=0.8)
        ax4.set_ylabel('Change in Area (%)', fontweight='bold')
        ax4.set_title(
            f'Vegetation Category Changes ({self.start_year} to {self.end_year})',
            fontsize=12,
            fontweight='bold'
        )
        ax4.tick_params(axis='x', rotation=45)
        ax4.grid(True, alpha=0.3)

        # Add value labels on change bars
        for bar in change_bars:
            height = bar.get_height()
            ax4.text(
                bar.get_x() + bar.get_width()/2,
                height + (0.5 if height > 0 else -1.0),
                f'{height:+.1f}%',
                ha='center',
                va='bottom' if height > 0 else 'top',
                fontsize=10,
                fontweight='bold'
            )

        plt.tight_layout()
        plt.savefig(save_path, dpi=100, bbox_inches='tight')

        # Generate preview/thumbnail (smaller resolution for cards)
        preview_path = save_path.replace('.png', '_preview.png')
        plt.savefig(preview_path, dpi=50, bbox_inches='tight')
        plt.close()

        print(f"✅ Charts saved to {save_path}")
        print(f"✅ Preview saved to {preview_path}")

    @staticmethod
    def classify_ndvi(ndvi_image: ee.Image) -> ee.Image:
        """
        Classify NDVI image into vegetation categories

        Args:
            ndvi_image: Earth Engine NDVI image

        Returns:
            Classified Earth Engine image
        """
        return (
            ee.Image(1)
            .where(ndvi_image.gte(0.2), 2)
            .where(ndvi_image.gte(0.4), 3)
            .where(ndvi_image.gte(0.6), 4)
            .rename('NDVI_Class')
        )

    @staticmethod
    def compute_statistics(
        ndvi_class_start: ee.Image,
        ndvi_class_end: ee.Image,
        aoi: ee.Geometry,
        start_year: int,
        end_year: int
    ) -> Dict[str, Any]:
        """
        Compute NDVI class statistics for both years

        Args:
            ndvi_class_start: Classified NDVI image for start year
            ndvi_class_end: Classified NDVI image for end year
            aoi: Area of interest geometry
            start_year: Start year
            end_year: End year

        Returns:
            Dictionary with statistics for both years
        """
        print("📈 Calculating Vegetation Index class statistics...")

        # Compute histograms
        hist_start = ndvi_class_start.reduceRegion(
            reducer=ee.Reducer.frequencyHistogram(),
            geometry=aoi,
            scale=500,  # Increased from 100m for faster processing
            maxPixels=1e9
        ).get('NDVI_Class').getInfo()

        hist_end = ndvi_class_end.reduceRegion(
            reducer=ee.Reducer.frequencyHistogram(),
            geometry=aoi,
            scale=500,  # Increased from 100m for faster processing
            maxPixels=1e9
        ).get('NDVI_Class').getInfo()

        # Process statistics
        total_start = sum(hist_start.values()) if hist_start else 1
        total_end = sum(hist_end.values()) if hist_end else 1

        stats_start = []
        stats_end = []

        print("\n📊 Vegetation Index Distribution Results:")
        print(f"{'Category':<30} {start_year:>10} {end_year:>10} {'Change':>10}")
        print("-"*60)

        for i in range(4):
            class_key = str(i + 1)
            pct_start = 100 * hist_start.get(class_key, 0) / total_start
            pct_end = 100 * hist_end.get(class_key, 0) / total_end
            change = pct_end - pct_start

            print(f"{VegetationVisualization.NDVI_CLASSES['labels'][i]:<30} "
                  f"{pct_start:>9.1f}% {pct_end:>9.1f}% {change:>+9.1f}%")

            stats_start.append({
                'label': VegetationVisualization.NDVI_CLASSES['labels'][i],
                'value': pct_start,
                'color': VegetationVisualization.NDVI_CLASSES['colors'][i]
            })

            stats_end.append({
                'label': VegetationVisualization.NDVI_CLASSES['labels'][i],
                'value': pct_end,
                'color': VegetationVisualization.NDVI_CLASSES['colors'][i]
            })

        print("-"*60)

        return {
            'start_year': stats_start,
            'end_year': stats_end
        }
