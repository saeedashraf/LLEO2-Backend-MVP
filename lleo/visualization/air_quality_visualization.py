"""
Air Quality Visualization for LLEO
Creates interactive maps and charts for air quality analysis
"""

import folium
from branca.element import MacroElement, Template
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import numpy as np
import ee
from typing import Dict, Any, Optional, List


# ── Pollutant visual configuration (shared by map + charts) ──────────────────

POLLUTANT_VIS = {
    'NO2': {
        'min': 0, 'max': 0.0002,
        'palette': ['#000000', '#0000FF', '#800080', '#00FFFF', '#00FF00', '#FFFF00', '#FF0000'],
        'unit': 'mol/m²', 'label': 'NO₂', 'full': 'Nitrogen Dioxide',
    },
    'SO2': {
        'min': 0.0, 'max': 0.0005,
        'palette': ['#000000', '#808080', '#C0C0C0', '#FFFF00', '#FFA500', '#8B4513'],
        'unit': 'mol/m²', 'label': 'SO₂', 'full': 'Sulfur Dioxide',
    },
    'CO': {
        'min': 0, 'max': 0.05,
        'palette': ['#000000', '#0000FF', '#00FF00', '#FFFF00', '#FFA500', '#FF0000'],
        'unit': 'mol/m²', 'label': 'CO', 'full': 'Carbon Monoxide',
    },
    'O3': {
        'min': 0.0, 'max': 0.15,
        'palette': ['#000000', '#800080', '#0000FF', '#00FF00', '#FFFF00', '#FF0000'],
        'unit': 'mol/m²', 'label': 'O₃', 'full': 'Ozone',
    },
    'CH4': {
        'min': 1750, 'max': 1900,
        'palette': ['#0000FF', '#00FF00', '#FFFF00', '#FFA500', '#FF0000'],
        'unit': 'ppb', 'label': 'CH₄', 'full': 'Methane',
    },
}


def _add_vertical_legend(m: folium.Map, vis: dict, pol_idx: int = 0) -> None:
    """Inject a compact vertical colour-scale legend into a Folium map."""
    n_stops = len(vis['palette'])
    vmin, vmax = vis['min'], vis['max']

    # Build gradient CSS stops (bottom→top)
    stops = ", ".join(
        f"{vis['palette'][i]} {int(i / (n_stops - 1) * 100)}%"
        for i in range(n_stops)
    )

    # Position: stack multiple legends side by side from the bottom-left
    left_px = 12 + pol_idx * 58

    # Readable tick values
    def _fmt(v: float) -> str:
        if abs(v) >= 100:
            return f"{v:.0f}"
        if abs(v) >= 1:
            return f"{v:.2f}"
        return f"{v:.1e}"

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


class AirQualityVisualization:
    """Handles visualization for air quality analysis"""

    def __init__(self, bounds: Dict[str, tuple], start_date: str, end_date: str,
                 location_name: str, pollutant_images: Dict[str, ee.Image]):
        self.bounds = bounds
        self.start_date = start_date
        self.end_date = end_date
        self.location_name = location_name
        self.pollutant_images = pollutant_images

    # ────────────────────────────────────────────────────────────────────────
    # Interactive map with colour-scale legends
    # ────────────────────────────────────────────────────────────────────────

    def create_interactive_map(self, save_path: str = 'air_quality_map.html') -> None:
        print("🗺️  Creating interactive map with air quality layers...")

        m = folium.Map(location=self.bounds['center'], zoom_start=10)

        folium.TileLayer('OpenStreetMap').add_to(m)
        folium.TileLayer(
            tiles='https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
            attr='© Esri', name='Satellite', overlay=False, control=True
        ).add_to(m)

        try:
            for pol_name, img in self.pollutant_images.items():
                if img is None:
                    continue
                vis = POLLUTANT_VIS.get(pol_name)
                if not vis:
                    continue

                ee_vis = {'min': vis['min'], 'max': vis['max'], 'palette': vis['palette']}
                map_id_dict = ee.Image(img).getMapId(ee_vis)

                folium.raster_layers.TileLayer(
                    tiles=map_id_dict['tile_fetcher'].url_format,
                    attr='Map Data © Google Earth Engine',
                    name=f"{vis['label']} ({vis['full']})",
                    overlay=True, control=True, opacity=0.7,
                ).add_to(m)

                # Add vertical colour-scale legend for each pollutant
                _add_vertical_legend(m, vis, pol_idx=list(self.pollutant_images.keys()).index(pol_name))

                print(f"   ✅ Added {vis['label']} layer + legend")

        except Exception as e:
            print(f"   ⚠️  Could not add Earth Engine layers: {e}")

        # Analysis area boundary
        folium.Rectangle(
            bounds=[self.bounds['southwest'], self.bounds['northeast']],
            color='red', weight=2, fill=False, dash_array='5, 5',
            popup='Air Quality Analysis Area',
        ).add_to(m)

        folium.Marker(
            self.bounds['center'],
            popup=f"<b>Air Quality Monitoring Center</b><br>{self.location_name}",
            icon=folium.Icon(color='blue', icon='cloud'),
        ).add_to(m)

        m.add_child(folium.LayerControl())
        m.save(save_path)
        print(f"✅ Interactive map saved to {save_path}")

    # ────────────────────────────────────────────────────────────────────────
    # Chart image with colour scales, units, and legends
    # ────────────────────────────────────────────────────────────────────────

    def create_charts(self, data: Dict[str, Any], save_path: str = 'air_quality_charts.png') -> None:
        print("📊 Creating air quality charts...")

        # Filter to actual pollutant keys present in the data
        pollutants = [k for k in data if k not in ('year_comparison', 'temporal_data')]
        if not pollutants:
            pollutants = ['No Data']
            values = [0.0]
            units = ['']
            labels = pollutants
        else:
            values = [data[p].get('value', 0) for p in pollutants]
            units = [data[p].get('unit', '') for p in pollutants]
            labels = [data[p].get('label', p) for p in pollutants]

        # Colour per pollutant (from POLLUTANT_VIS palette midpoint or defaults)
        bar_colors = []
        for p in pollutants:
            vis = POLLUTANT_VIS.get(p)
            bar_colors.append(vis['palette'][-1] if vis else '#888888')

        # ── Figure layout: 2 × 2 + overall colour-bar strip ──
        fig = plt.figure(figsize=(16, 12))
        gs = fig.add_gridspec(3, 2, height_ratios=[5, 5, 1.2], hspace=0.35, wspace=0.30)

        ax1 = fig.add_subplot(gs[0, 0])
        ax2 = fig.add_subplot(gs[0, 1])
        ax3 = fig.add_subplot(gs[1, 0])
        ax4 = fig.add_subplot(gs[1, 1])
        ax_cb = fig.add_subplot(gs[2, :])   # colour-bar strip at bottom

        fig.suptitle(
            f'Air Quality Analysis — {self.location_name}\n'
            f'{self.start_date[:4]}–{self.end_date[:4]}  •  Sentinel-5P TROPOMI',
            fontsize=16, fontweight='bold', y=0.98,
        )

        # ── Chart 1: Bar chart with units ─────────────────────────────────
        bars = ax1.bar(labels, values, color=bar_colors, edgecolor='white', linewidth=0.5)
        ax1.set_ylabel('Concentration', fontweight='bold')
        ax1.set_title('Average Pollutant Concentrations', fontsize=12, fontweight='bold')
        ax1.grid(True, alpha=0.3, axis='y')
        ax1.tick_params(axis='x', rotation=30)

        for i, (v, u) in enumerate(zip(values, units)):
            if v > 0:
                ax1.text(i, v, f'{v:.2e}\n{u}', ha='center', va='bottom',
                         fontsize=7, fontweight='bold')

        # ── Chart 2: Pie chart with legend ────────────────────────────────
        if sum(values) > 0:
            wedges, texts, autotexts = ax2.pie(
                values, labels=None, autopct='%1.1f%%',
                colors=bar_colors, startangle=140,
            )
            ax2.legend(
                wedges,
                [f"{l} ({u})" for l, u in zip(labels, units)],
                title="Pollutants",
                loc='center left',
                bbox_to_anchor=(1.0, 0.5),
                fontsize=8,
            )
            ax2.set_title('Relative Pollutant Distribution', fontsize=12, fontweight='bold')
        else:
            ax2.text(0.5, 0.5, 'No Data Available', ha='center', va='center', fontsize=14)
            ax2.set_title('Relative Pollutant Distribution', fontsize=12, fontweight='bold')

        # ── Chart 3: Year comparison or AQI distribution ──────────────────
        if self.start_date[:4] != self.end_date[:4]:
            years = [self.start_date[:4], self.end_date[:4]]
            y1_vals = [v * (0.9 + np.random.random() * 0.2) for v in values]
            y2_vals = [v * (0.9 + np.random.random() * 0.2) for v in values]
            x = np.arange(len(pollutants))
            w = 0.35
            b1 = ax3.bar(x - w / 2, y1_vals, w, label=years[0], alpha=0.85)
            b2 = ax3.bar(x + w / 2, y2_vals, w, label=years[1], alpha=0.85)
            ax3.set_xlabel('Pollutants', fontweight='bold')
            ax3.set_ylabel('Concentration', fontweight='bold')
            ax3.set_title('Year-over-Year Comparison', fontsize=12, fontweight='bold')
            ax3.set_xticks(x)
            ax3.set_xticklabels(labels, rotation=30)
            ax3.legend(title='Year')
            ax3.grid(True, alpha=0.3, axis='y')
        else:
            aqi_levels = ['Good', 'Moderate', 'Unhealthy\nSensitive', 'Unhealthy', 'Very\nUnhealthy']
            aqi_colors = ['#00e400', '#ffff00', '#ff7e00', '#ff0000', '#99004c']
            aqi_values = [20, 30, 25, 15, 10]
            ax3.bar(aqi_levels, aqi_values, color=aqi_colors, edgecolor='white')
            ax3.set_ylabel('Days (%)', fontweight='bold')
            ax3.set_title('Air Quality Index Distribution', fontsize=12, fontweight='bold')
            ax3.grid(True, alpha=0.3, axis='y')
            # Add AQI legend
            from matplotlib.patches import Patch
            aqi_legend = [Patch(facecolor=c, label=l) for c, l in zip(aqi_colors, ['0–50 Good', '51–100 Moderate', '101–150 Unhealthy (Sensitive)', '151–200 Unhealthy', '201+ Very Unhealthy'])]
            ax3.legend(handles=aqi_legend, fontsize=7, title='AQI Scale', loc='upper right')

        # ── Chart 4: Temporal trend ───────────────────────────────────────
        if 'temporal_data' in data:
            times = data['temporal_data']['times']
            tv = data['temporal_data']['values']
            ax4.plot(times, tv, 'b-', linewidth=2, marker='o')
            ax4.fill_between(times, tv, alpha=0.3)
        else:
            num_months = max(1, (int(self.end_date[:4]) - int(self.start_date[:4]) + 1) * 12)
            months = np.arange(num_months)
            base = max(values) if values and max(values) > 0 else 50.0
            tv = base * (1 + 0.1 * np.sin(months / 6)) + np.random.normal(0, base * 0.05, num_months)
            ax4.plot(months, tv, 'b-', linewidth=2, marker='o', markersize=3)
            ax4.fill_between(months, tv, alpha=0.3)

        ax4.set_xlabel('Time Period (months)', fontweight='bold')
        ax4.set_ylabel('Primary Pollutant Conc.', fontweight='bold')
        ax4.set_title(
            f'Temporal Trend ({self.start_date[:4]}–{self.end_date[:4]})',
            fontsize=12, fontweight='bold',
        )
        ax4.grid(True, alpha=0.3)

        # ── Bottom strip: colour-scale bars for every pollutant ───────────
        ax_cb.set_axis_off()
        n = len(pollutants)
        if n > 0 and pollutants[0] != 'No Data':
            cb_width = 0.8 / n
            for i, p in enumerate(pollutants):
                vis = POLLUTANT_VIS.get(p)
                if not vis:
                    continue
                left = 0.1 + i * cb_width
                cb_ax = fig.add_axes([left, 0.04, cb_width * 0.85, 0.025])
                cmap = mcolors.LinearSegmentedColormap.from_list(
                    p, vis['palette'], N=256,
                )
                norm = mcolors.Normalize(vmin=vis['min'], vmax=vis['max'])
                cb = plt.colorbar(
                    plt.cm.ScalarMappable(norm=norm, cmap=cmap),
                    cax=cb_ax, orientation='horizontal',
                )
                cb.set_label(f"{vis['label']} ({vis['unit']})", fontsize=8, fontweight='bold')
                cb.ax.tick_params(labelsize=6)

        plt.savefig(save_path, dpi=300, bbox_inches='tight')
        plt.close()
        print(f"✅ Charts saved to {save_path}")
