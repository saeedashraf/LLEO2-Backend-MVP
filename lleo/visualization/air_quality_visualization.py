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

        def _no_data(ax, title, note='No valid data for this period'):
            """Honest empty panel. Never invent numbers to fill a chart."""
            ax.text(0.5, 0.5, note, ha='center', va='center',
                    fontsize=12, color='#6b7280')
            ax.set_xticks([])
            ax.set_yticks([])
            ax.set_title(title, fontsize=12, fontweight='bold')

        # Filter to actual pollutant keys present in the data
        pollutants = [k for k in data if k not in ('year_comparison', 'temporal_data')]

        # value is None when every pixel in the region was masked. That is NOT
        # the same as a concentration of zero and must not be plotted as one.
        raw_values = {p: data[p].get('value') for p in pollutants}
        units = {p: data[p].get('unit', '') for p in pollutants}
        labels = {p: data[p].get('label', p) for p in pollutants}
        measured = [p for p in pollutants if raw_values.get(p) is not None]

        # Distinct categorical colours. POLLUTANT_VIS palettes are sequential
        # ramps whose last stop is red for several pollutants, which made
        # NO2 and CO indistinguishable in the pie chart.
        CHART_COLORS = {
            'NO2': '#d7342a',
            'SO2': '#8c564b',
            'CO':  '#1f77b4',
            'O3':  '#2ca02c',
            'CH4': '#e377c2',
        }
        _fallback = ['#7f7f7f', '#bcbd22', '#17becf', '#9467bd', '#ff7f0e']
        bar_colors_all = {}
        for i, p in enumerate(pollutants):
            bar_colors_all[p] = CHART_COLORS.get(p, _fallback[i % len(_fallback)])

        fig = plt.figure(figsize=(16, 12))
        gs = fig.add_gridspec(3, 2, height_ratios=[5, 5, 1.2], hspace=0.35, wspace=0.30)
        ax1 = fig.add_subplot(gs[0, 0])
        ax2 = fig.add_subplot(gs[0, 1])
        ax3 = fig.add_subplot(gs[1, 0])
        ax4 = fig.add_subplot(gs[1, 1])
        ax_cb = fig.add_subplot(gs[2, :])

        fig.suptitle(
            'Air Quality Analysis \u2014 {}\n{}\u2013{}  \u2022  Sentinel-5P TROPOMI'.format(
                self.location_name, self.start_date[:4], self.end_date[:4]),
            fontsize=16, fontweight='bold', y=0.98,
        )

        # Chart 1: average concentration per pollutant
        if measured:
            vals = [raw_values[p] for p in measured]
            ax1.bar(range(len(measured)), vals,
                    color=[bar_colors_all[p] for p in measured],
                    edgecolor='white', linewidth=0.5, width=0.6)
            ax1.set_ylabel('Concentration', fontweight='bold')
            ax1.set_title('Average Pollutant Concentrations', fontsize=12, fontweight='bold')
            ax1.grid(True, alpha=0.3, axis='y')
            # Short tick labels: the long descriptive label used to be rotated
            # 30 degrees and overlapped the title of the panel below.
            ax1.set_xticks(range(len(measured)))
            ax1.set_xticklabels(measured, fontsize=10, fontweight='bold')
            ax1.set_xlim(-0.6, len(measured) - 0.4)
            # Headroom so the value annotation is not clipped by the top spine.
            ax1.set_ylim(0, max(vals) * 1.18)
            for i, p in enumerate(measured):
                ax1.text(i, raw_values[p],
                         '{:.4g} {}'.format(raw_values[p], units[p]),
                         ha='center', va='bottom', fontsize=9, fontweight='bold')
        else:
            _no_data(ax1, 'Average Pollutant Concentrations',
                     'No valid retrievals in this\nregion and time period')

        # Chart 2: relative distribution (needs 2+ measured pollutants)
        if len(measured) >= 2:
            vals = [raw_values[p] for p in measured]
            wedges, _, _ = ax2.pie(
                vals, labels=None, autopct='%1.1f%%',
                colors=[bar_colors_all[p] for p in measured], startangle=140,
            )
            ax2.legend(
                wedges,
                ['{} ({})'.format(labels[p], units[p]) for p in measured],
                title='Pollutants', loc='center left',
                bbox_to_anchor=(1.0, 0.5), fontsize=8,
            )
            ax2.set_title('Relative Pollutant Distribution', fontsize=12, fontweight='bold')
        else:
            _no_data(ax2, 'Relative Pollutant Distribution',
                     'Needs at least two measured\npollutants to compare')

        # Real monthly series produced by the processor:
        #   [{'label': 'YYYY-MM', 'value': float or None}, ...]
        series = {}
        for p in pollutants:
            series[p] = [m for m in (data[p].get('monthly') or [])
                         if m.get('value') is not None]
        primary = max(measured, key=lambda p: len(series.get(p, [])), default=None)

        # Chart 3: year-over-year, computed from the monthly series.
        # This previously multiplied the single mean by random noise to
        # fabricate two years of "comparison" data.
        yearly = {}
        if primary:
            for m in series[primary]:
                yearly.setdefault(str(m['label'])[:4], []).append(m['value'])

        if len(yearly) >= 2:
            years = sorted(yearly)
            means = [sum(yearly[y]) / len(yearly[y]) for y in years]
            colour = bar_colors_all.get(primary, '#888888')
            ax3.plot(range(len(years)), means, 'o', markersize=16,
                     color=colour, markeredgecolor='white', markeredgewidth=2)
            for i, y in enumerate(years):
                ax3.text(i, means[i],
                         '  {:.4g}  ({} mo)'.format(means[i], len(yearly[y])),
                         ha='left', va='center', fontsize=9, fontweight='bold')
            ax3.set_xticks(range(len(years)))
            ax3.set_xticklabels(years, fontsize=11, fontweight='bold')
            ax3.set_xlim(-0.5, len(years) - 0.2)
            # Zoom to the data. A bar chart truncated this way would overstate
            # the difference; markers on a zoomed axis do not.
            lo, hi = min(means), max(means)
            pad = max((hi - lo) * 0.8, abs(hi) * 0.002) or 1.0
            ax3.set_ylim(lo - pad, hi + pad)
            ax3.set_xlabel('Year', fontweight='bold')
            ax3.set_ylabel('{} ({})'.format(labels[primary], units[primary]),
                           fontweight='bold')
            ax3.set_title('Year-over-Year Comparison (annual mean)',
                          fontsize=12, fontweight='bold')
            ax3.grid(True, alpha=0.3)
        else:
            _no_data(ax3, 'Year-over-Year Comparison',
                     'Needs valid data in two or\nmore calendar years')

        # Chart 4: temporal trend from the real monthly series.
        # This previously drew a sine wave plus Gaussian noise around an
        # arbitrary base of 50.0 whenever no series was supplied.
        if primary and len(series[primary]) >= 2:
            pts = series[primary]
            xs = list(range(len(pts)))
            ys = [m['value'] for m in pts]
            lo, hi = min(ys), max(ys)
            pad = max((hi - lo) * 0.25, abs(hi) * 0.002) or 1.0
            y_floor = lo - pad
            ax4.plot(xs, ys, 'b-', linewidth=2, marker='o', markersize=4)
            # Fill down to the axis floor, not to zero: filling from zero made
            # a series spanning 1850-1950 ppb look like a flat block.
            ax4.fill_between(xs, ys, y_floor, alpha=0.25)
            ax4.set_ylim(y_floor, hi + pad)
            step = max(1, len(pts) // 8)
            ax4.set_xticks(xs[::step])
            ax4.set_xticklabels([m['label'] for m in pts][::step],
                                rotation=45, fontsize=8)
            ax4.set_xlabel('Month', fontweight='bold')
            ax4.set_ylabel('{} ({})'.format(labels[primary], units[primary]),
                           fontweight='bold')
            ax4.set_title(
                'Temporal Trend \u2014 {} ({} months with data)'.format(
                    labels[primary], len(pts)),
                fontsize=12, fontweight='bold',
            )
            ax4.grid(True, alpha=0.3)
        else:
            _no_data(ax4,
                     'Temporal Trend ({}\u2013{})'.format(
                         self.start_date[:4], self.end_date[:4]),
                     'Fewer than two months with\nvalid retrievals')

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
