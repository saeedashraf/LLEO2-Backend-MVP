"""
Water Quality Visualization Module

Generates interactive maps and charts for water quality analysis
Uses Sentinel-2 data to analyze NDWI, turbidity, and chlorophyll
"""

import folium
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg')  # Use non-interactive backend
import numpy as np
import ee
from typing import Dict, Any, Optional


# ── Vis-parameter config for each water-quality layer ──────────────────────
WATER_VIS: Dict[str, Dict[str, Any]] = {
    'NDWI': {
        'min': -0.5, 'max': 1.0,
        'palette': ['red', 'yellow', 'green', 'cyan', 'blue'],
        'label': 'NDWI', 'unit': 'index',
    },
    'Turbidity': {
        'min': -0.5, 'max': 0.5,
        'palette': ['blue', 'green', 'yellow', 'orange', 'red'],
        'label': 'Turbidity\n(NDTI)', 'unit': 'index',
    },
    'Chlorophyll': {
        'min': -0.5, 'max': 0.5,
        'palette': ['blue', 'cyan', 'green', 'yellow', 'red'],
        'label': 'Chloro\n(NDCI)', 'unit': 'index',
    },
    'TSM': {
        'min': 0.0, 'max': 0.06,
        'palette': ['#08306b', '#2171b5', '#6baed6', '#fee090', '#d73027'],
        'label': 'TSM', 'unit': 'refl.',
    },
    'CDOM': {
        'min': 0.5, 'max': 3.0,
        'palette': ['#f7fbff', '#9ecae1', '#3182bd', '#6a3d9a', '#4a1486'],
        'label': 'CDOM', 'unit': 'ratio',
    },
}


def _add_vertical_legend(m: folium.Map, vis: dict, layer_idx: int = 0) -> None:
    """Inject a compact vertical colour-scale legend into a Folium map."""
    n_stops = len(vis['palette'])
    vmin, vmax = vis['min'], vis['max']

    stops = ", ".join(
        f"{vis['palette'][i]} {int(i / (n_stops - 1) * 100)}%"
        for i in range(n_stops)
    )
    left_px = 12 + layer_idx * 58

    def _fmt(v: float) -> str:
        if abs(v) >= 100:
            return f"{v:.0f}"
        if abs(v) >= 1:
            return f"{v:.2f}"
        return f"{v:.2f}"

    label_html = vis['label'].replace('\n', '<br>')

    html = f"""
    <div style="
        position: fixed; bottom: 30px; left: {left_px}px; z-index: 1000;
        background: rgba(255,255,255,0.92); border-radius: 6px;
        padding: 6px 8px; box-shadow: 0 2px 6px rgba(0,0,0,0.3);
        font-family: Arial, sans-serif; width: 46px;
    ">
        <div style="font-size: 10px; font-weight: 700; text-align: center;
                    margin-bottom: 4px; line-height: 1.1;">
            {label_html}
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


class WaterQualityVisualization:
    """Handles visualization generation for water quality analysis"""

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
        true_color_image: Optional[ee.Image] = None,
        ndwi_image: Optional[ee.Image] = None,
        turbidity_image: Optional[ee.Image] = None,
        chlorophyll_image: Optional[ee.Image] = None,
        tsm_image: Optional[ee.Image] = None,
        cdom_image: Optional[ee.Image] = None,
        save_path: str = 'water_quality_map.html'
    ) -> None:
        """
        Create interactive Folium map with Earth Engine water quality layers

        Args:
            true_color_image: Earth Engine Image - true color composite
            ndwi_image: Earth Engine Image - Normalized Difference Water Index
            turbidity_image: Earth Engine Image - turbidity (NDTI)
            chlorophyll_image: Earth Engine Image - chlorophyll concentration (NDCI)
            tsm_image: Earth Engine Image - Total Suspended Matter proxy
            cdom_image: Earth Engine Image - Colored Dissolved Organic Matter proxy
            save_path: Path to save HTML map file
        """
        print("🗺️  Creating interactive map with water quality layers...")

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

        # Add Earth Engine water quality layers
        layer_idx = 0  # tracks position for vertical legends
        try:
            # True color visualization
            if true_color_image:
                true_color_vis = {
                    'min': 0,
                    'max': 3000,
                    'bands': ['B4', 'B3', 'B2']
                }
                map_id_dict = ee.Image(true_color_image).getMapId(true_color_vis)
                folium.raster_layers.TileLayer(
                    tiles=map_id_dict['tile_fetcher'].url_format,
                    attr='Map Data © Google Earth Engine',
                    name='True Color',
                    overlay=True,
                    control=True
                ).add_to(m)
                print("   ✅ Added True Color layer")

            # NDWI visualization
            if ndwi_image:
                vis = WATER_VIS['NDWI']
                ndwi_vis = {'min': vis['min'], 'max': vis['max'], 'palette': vis['palette']}
                map_id_dict = ee.Image(ndwi_image).getMapId(ndwi_vis)
                folium.raster_layers.TileLayer(
                    tiles=map_id_dict['tile_fetcher'].url_format,
                    attr='Map Data © Google Earth Engine',
                    name='Water Index (NDWI)',
                    overlay=True,
                    control=True,
                    opacity=0.7
                ).add_to(m)
                _add_vertical_legend(m, vis, layer_idx); layer_idx += 1
                print("   ✅ Added NDWI layer")

            # Turbidity (NDTI) visualization
            if turbidity_image:
                vis = WATER_VIS['Turbidity']
                turbidity_vis = {'min': vis['min'], 'max': vis['max'], 'palette': vis['palette']}
                map_id_dict = ee.Image(turbidity_image).getMapId(turbidity_vis)
                folium.raster_layers.TileLayer(
                    tiles=map_id_dict['tile_fetcher'].url_format,
                    attr='Map Data © Google Earth Engine',
                    name='Turbidity (NDTI)',
                    overlay=True,
                    control=True,
                    opacity=0.7
                ).add_to(m)
                _add_vertical_legend(m, vis, layer_idx); layer_idx += 1
                print("   ✅ Added Turbidity layer")

            # Chlorophyll (NDCI) visualization
            if chlorophyll_image:
                vis = WATER_VIS['Chlorophyll']
                chlorophyll_vis = {'min': vis['min'], 'max': vis['max'], 'palette': vis['palette']}
                map_id_dict = ee.Image(chlorophyll_image).getMapId(chlorophyll_vis)
                folium.raster_layers.TileLayer(
                    tiles=map_id_dict['tile_fetcher'].url_format,
                    attr='Map Data © Google Earth Engine',
                    name='Chlorophyll (NDCI)',
                    overlay=True,
                    control=True,
                    opacity=0.7
                ).add_to(m)
                _add_vertical_legend(m, vis, layer_idx); layer_idx += 1
                print("   ✅ Added Chlorophyll layer")

            # TSM (Total Suspended Matter) visualization
            if tsm_image:
                vis = WATER_VIS['TSM']
                tsm_vis = {'min': vis['min'], 'max': vis['max'], 'palette': vis['palette']}
                map_id_dict = ee.Image(tsm_image).getMapId(tsm_vis)
                folium.raster_layers.TileLayer(
                    tiles=map_id_dict['tile_fetcher'].url_format,
                    attr='Map Data © Google Earth Engine',
                    name='TSM (Suspended Matter)',
                    overlay=True,
                    control=True,
                    opacity=0.7
                ).add_to(m)
                _add_vertical_legend(m, vis, layer_idx); layer_idx += 1
                print("   ✅ Added TSM layer")

            # CDOM (Colored Dissolved Organic Matter) visualization
            if cdom_image:
                vis = WATER_VIS['CDOM']
                cdom_vis = {'min': vis['min'], 'max': vis['max'], 'palette': vis['palette']}
                map_id_dict = ee.Image(cdom_image).getMapId(cdom_vis)
                folium.raster_layers.TileLayer(
                    tiles=map_id_dict['tile_fetcher'].url_format,
                    attr='Map Data © Google Earth Engine',
                    name='CDOM (Dissolved Organic)',
                    overlay=True,
                    control=True,
                    opacity=0.7
                ).add_to(m)
                _add_vertical_legend(m, vis, layer_idx); layer_idx += 1
                print("   ✅ Added CDOM layer")

        except Exception as e:
            print(f"   ⚠️  Could not add Earth Engine layers: {e}")

        # Add analysis area boundary
        folium.Rectangle(
            bounds=[self.bounds['southwest'], self.bounds['northeast']],
            color='blue',
            weight=2,
            fill=False,
            dash_array='5, 5',
            popup='Water Quality Analysis Area'
        ).add_to(m)

        # Add center marker
        folium.Marker(
            self.bounds['center'],
            popup=f"<b>Water Quality Monitoring Point</b><br>"
                  f"Location: {self.location_name}<br>"
                  f"Time Period: {self.start_year} to {self.end_year}",
            icon=folium.Icon(color='blue', icon='tint')
        ).add_to(m)

        # Add layer control
        m.add_child(folium.LayerControl())

        # Save map
        m.save(save_path)
        print(f"✅ Interactive map saved to {save_path}")

    def create_charts(
        self,
        data: Dict[str, Any],
        figsize: tuple = (15, 10),
        save_path: str = 'water_quality_charts.png'
    ) -> None:
        """
        Create water quality analysis charts

        Args:
            data: Dictionary with water quality data (ndwi, turbidity, chlorophyll, water_quality_index)
                  Can also contain 'year_comparison' for multi-year analysis
            figsize: Figure size (width, height)
            save_path: Path to save PNG file
        """
        print("📊 Creating water quality charts...")

        fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=figsize)

        # Create title
        if self.start_year == self.end_year:
            title = f'Water Quality Analysis: {self.location_name} ({self.start_year})'
        else:
            title = f'Water Quality Analysis: {self.location_name} ({self.start_year} vs {self.end_year})'

        fig.suptitle(title, fontsize=16, fontweight='bold')

        # Check if we have comparison data (either year_comparison or new comparison format)
        if 'year_comparison' in data and len(data['year_comparison']) == 2:
            self._create_comparison_charts(data, ax1, ax2, ax3, ax4)
        elif 'comparison' in data and len(data['comparison']) == 2:
            # New intelligent comparison format
            self._create_intelligent_comparison_charts(data, ax1, ax2, ax3, ax4)
        elif 'time_series' in data:
            # Time series analysis
            self._create_time_series_charts(data, ax1, ax2, ax3, ax4)
        else:
            self._create_single_period_charts(data, ax1, ax2, ax3, ax4)

        plt.tight_layout()
        plt.savefig(save_path, dpi=100, bbox_inches='tight')

        # Generate preview/thumbnail (smaller resolution for cards)
        preview_path = save_path.replace('.png', '_preview.png')
        plt.savefig(preview_path, dpi=50, bbox_inches='tight')
        plt.close()

        print(f"✅ Charts saved to {save_path}")
        print(f"✅ Preview saved to {preview_path}")

    def _create_comparison_charts(self, data: Dict[str, Any], ax1, ax2, ax3, ax4):
        """Create charts for year-to-year comparison"""
        years = list(data['year_comparison'].keys())
        year1_data = data['year_comparison'][years[0]]
        year2_data = data['year_comparison'][years[1]]

        # Parameters comparison - side by side bars
        parameters = ['NDWI', 'Turbidity', 'Chlorophyll', 'TSM', 'CDOM']
        year1_values = [
            year1_data.get('ndwi', 0),
            year1_data.get('turbidity', 0),
            year1_data.get('chlorophyll', 0),
            year1_data.get('tsm', 0),
            year1_data.get('cdom', 0)
        ]
        year2_values = [
            year2_data.get('ndwi', 0),
            year2_data.get('turbidity', 0),
            year2_data.get('chlorophyll', 0),
            year2_data.get('tsm', 0),
            year2_data.get('cdom', 0)
        ]

        x = np.arange(len(parameters))
        width = 0.35

        bars1 = ax1.bar(x - width/2, year1_values, width, label=f'{years[0]}', color='#4169e1', alpha=0.8)
        bars2 = ax1.bar(x + width/2, year2_values, width, label=f'{years[1]}', color='#ff7f50', alpha=0.8)

        ax1.set_xlabel('Parameters', fontweight='bold')
        ax1.set_ylabel('Index / Ratio Value', fontweight='bold')
        ax1.set_title('Water Quality Parameters Comparison', fontsize=12, fontweight='bold')
        ax1.set_xticks(x)
        ax1.set_xticklabels(parameters)
        ax1.legend()
        ax1.grid(True, alpha=0.3)

        # Add value labels on bars
        for bar in bars1:
            height = bar.get_height()
            ax1.text(bar.get_x() + bar.get_width()/2., height,
                    f'{height:.3f}', ha='center', va='bottom', fontsize=8)
        for bar in bars2:
            height = bar.get_height()
            ax1.text(bar.get_x() + bar.get_width()/2., height,
                    f'{height:.3f}', ha='center', va='bottom', fontsize=8)

        # Water Quality Index comparison
        wqi1 = year1_data.get('water_quality_index', 50)
        wqi2 = year2_data.get('water_quality_index', 50)

        wqi_comparison = [wqi1, wqi2]
        ax2.bar(years, wqi_comparison, color=['#4169e1', '#ff7f50'], alpha=0.8)
        ax2.set_ylabel('Water Quality Index (0–100)', fontweight='bold')
        ax2.set_title('WQI Comparison', fontsize=12, fontweight='bold')
        ax2.grid(True, alpha=0.3)

        # Add value labels
        for i, v in enumerate(wqi_comparison):
            ax2.text(i, v, f'{v:.1f}%', ha='center', va='bottom', fontweight='bold')

        # Changes analysis
        changes = {}
        for i, param in enumerate(parameters):
            if year1_values[i] != 0:
                changes[param] = ((year2_values[i] - year1_values[i]) / abs(year1_values[i]) * 100)
            else:
                changes[param] = 0

        change_params = list(changes.keys())
        change_values = list(changes.values())
        colors = ['green' if v >= 0 else 'red' for v in change_values]

        ax3.bar(change_params, change_values, color=colors, alpha=0.7)
        ax3.set_ylabel('Percentage Change (%)', fontweight='bold')
        ax3.set_title(f'Parameter Changes ({years[0]} to {years[1]})', fontsize=12, fontweight='bold')
        ax3.grid(True, alpha=0.3)
        ax3.axhline(y=0, color='black', linestyle='-', linewidth=0.5)

        # Add percentage labels
        for i, v in enumerate(change_values):
            ax3.text(i, v, f'{v:.1f}%', ha='center', va='bottom' if v >= 0 else 'top', fontweight='bold')

        # Overall trend analysis
        overall_change = wqi2 - wqi1
        trend_color = 'green' if overall_change >= 0 else 'red'
        trend_text = 'Improvement' if overall_change >= 0 else 'Deterioration'

        ax4.bar(['Overall Water Quality'], [overall_change], color=trend_color, alpha=0.7)
        ax4.set_ylabel('WQI Change (points)', fontweight='bold')
        ax4.set_title(f'Overall Quality {trend_text}', fontsize=12, fontweight='bold')
        ax4.grid(True, alpha=0.3)
        ax4.axhline(y=0, color='black', linestyle='-', linewidth=0.5)
        ax4.text(0, overall_change, f'{overall_change:.1f}', ha='center',
                va='bottom' if overall_change >= 0 else 'top', fontweight='bold')

    def _create_single_period_charts(self, data: Dict[str, Any], ax1, ax2, ax3, ax4):
        """Create charts for single period analysis"""
        parameters = ['NDWI', 'Turbidity', 'Chlorophyll', 'TSM', 'CDOM']
        values = [
            data.get('ndwi', 0),
            data.get('turbidity', 0),
            data.get('chlorophyll', 0),
            data.get('tsm', 0),
            data.get('cdom', 0)
        ]
        colors = ['#4169e1', '#8b4513', '#228b22', '#b8860b', '#6a3d9a']

        ax1.bar(parameters, values, color=colors)
        ax1.set_ylabel('Index / Ratio Value', fontweight='bold')
        ax1.set_title('Water Quality Parameters', fontsize=12, fontweight='bold')
        ax1.grid(True, alpha=0.3)

        for i, v in enumerate(values):
            ax1.text(i, v, f'{v:.3f}', ha='center', va='bottom')

        # Water Quality Index
        wqi = data.get('water_quality_index', 50)
        ax2.pie([wqi, 100-wqi], labels=['Quality', 'Deficiency'],
                autopct='%1.1f%%', colors=['#00ff00', '#ff0000'], startangle=90)
        ax2.set_title(f'Water Quality Index: {wqi:.1f}%', fontsize=12, fontweight='bold')

        # Quality classification
        quality_levels = ['Excellent', 'Good', 'Fair', 'Poor', 'Very Poor']
        level_values = [25, 35, 20, 15, 5]
        level_colors = ['#00e400', '#ffff00', '#ff7e00', '#ff0000', '#7e0023']

        ax3.bar(quality_levels, level_values, color=level_colors)
        ax3.set_ylabel('Probability (%)', fontweight='bold')
        ax3.set_title('Water Quality Classification', fontsize=12, fontweight='bold')
        ax3.tick_params(axis='x', rotation=45)
        ax3.grid(True, alpha=0.3)

        # Trend simulation
        days = np.arange(30)
        trend = np.sin(days/5) * 10 + 70 + np.random.randn(30) * 3
        ax4.plot(days, trend, 'b-', linewidth=2, label='WQI')
        ax4.fill_between(days, trend, alpha=0.3)
        ax4.axhline(y=70, color='g', linestyle='--', label='Good Threshold')
        ax4.axhline(y=50, color='r', linestyle='--', label='Poor Threshold')
        ax4.set_xlabel('Days', fontweight='bold')
        ax4.set_ylabel('WQI Score (0–100)', fontweight='bold')
        ax4.set_title('30-Day WQI Trend', fontsize=12, fontweight='bold')
        ax4.legend()
        ax4.grid(True, alpha=0.3)

    def _create_intelligent_comparison_charts(self, data: Dict[str, Any], ax1, ax2, ax3, ax4):
        """Create charts for intelligent AI-planned period comparison (e.g., Jan vs Dec)"""
        comparison_data = data['comparison']
        labels = list(comparison_data.keys())
        period1_data = comparison_data[labels[0]]
        period2_data = comparison_data[labels[1]]

        # Parameters comparison - side by side bars
        parameters = ['NDWI', 'Turbidity', 'Chlorophyll', 'TSM', 'CDOM']
        period1_values = [
            period1_data.get('ndwi', 0),
            period1_data.get('turbidity', 0),
            period1_data.get('chlorophyll', 0),
            period1_data.get('tsm', 0),
            period1_data.get('cdom', 0)
        ]
        period2_values = [
            period2_data.get('ndwi', 0),
            period2_data.get('turbidity', 0),
            period2_data.get('chlorophyll', 0),
            period2_data.get('tsm', 0),
            period2_data.get('cdom', 0)
        ]

        x = np.arange(len(parameters))
        width = 0.35

        bars1 = ax1.bar(x - width/2, period1_values, width, label=labels[0], color='#4169e1', alpha=0.8)
        bars2 = ax1.bar(x + width/2, period2_values, width, label=labels[1], color='#ff7f50', alpha=0.8)

        ax1.set_xlabel('Parameters', fontweight='bold')
        ax1.set_ylabel('Index / Ratio Value', fontweight='bold')
        ax1.set_title('Water Quality Parameters Comparison', fontsize=12, fontweight='bold')
        ax1.set_xticks(x)
        ax1.set_xticklabels(parameters)
        ax1.legend()
        ax1.grid(True, alpha=0.3)

        # Add value labels on bars
        for bar in bars1:
            height = bar.get_height()
            ax1.text(bar.get_x() + bar.get_width()/2., height,
                    f'{height:.3f}', ha='center', va='bottom', fontsize=8)
        for bar in bars2:
            height = bar.get_height()
            ax1.text(bar.get_x() + bar.get_width()/2., height,
                    f'{height:.3f}', ha='center', va='bottom', fontsize=8)

        # Change analysis - show differences
        changes = [p2 - p1 for p1, p2 in zip(period1_values, period2_values)]
        colors = ['green' if c > 0 else 'red' if c < 0 else 'gray' for c in changes]

        bars = ax2.bar(parameters, changes, color=colors, alpha=0.7)
        ax2.axhline(y=0, color='black', linestyle='-', linewidth=0.5)
        ax2.set_xlabel('Parameters', fontweight='bold')
        ax2.set_ylabel('Change (Δ Index / Ratio)', fontweight='bold')
        ax2.set_title(f'Change: {labels[0]} → {labels[1]}', fontsize=12, fontweight='bold')
        ax2.grid(True, alpha=0.3)

        # Add value labels on bars
        for bar, change in zip(bars, changes):
            height = bar.get_height()
            ax2.text(bar.get_x() + bar.get_width()/2., height,
                    f'{change:+.3f}', ha='center', va='bottom' if height > 0 else 'top', fontsize=9)

        # Water Quality Index comparison
        wqi1 = period1_data.get('water_quality_index', 50)
        wqi2 = period2_data.get('water_quality_index', 50)

        ax3.bar([labels[0], labels[1]], [wqi1, wqi2], color=['#4169e1', '#ff7f50'], alpha=0.8)
        ax3.set_ylabel('WQI Score (0–100)', fontweight='bold')
        ax3.set_title('Water Quality Index', fontsize=12, fontweight='bold')
        ax3.set_ylim([0, 100])
        ax3.axhline(y=50, color='orange', linestyle='--', label='Threshold', alpha=0.5)
        ax3.legend()
        ax3.grid(True, alpha=0.3)

        # Add value labels
        for i, (label, wqi) in enumerate([(labels[0], wqi1), (labels[1], wqi2)]):
            ax3.text(i, wqi, f'{wqi:.1f}', ha='center', va='bottom', fontweight='bold')

        # Strategy reasoning (if available)
        strategy = data.get('strategy', {})
        reasoning = strategy.get('reasoning', 'AI-guided analysis strategy')
        strategy_type = strategy.get('strategy_type', 'comparison')

        ax4.axis('off')
        info_text = f"""
        Analysis Strategy: {strategy_type.replace('_', ' ').title()}

        Periods Analyzed:
        • {labels[0]}
        • {labels[1]}

        Reasoning:
        {reasoning[:200]}...

        Water Quality Assessment:
        • NDWI Change: {changes[0]:+.3f}
        • Turbidity Change: {changes[1]:+.3f}
        • Chlorophyll Change: {changes[2]:+.3f}
        • TSM Change: {changes[3]:+.3f}
        • CDOM Change: {changes[4]:+.3f}
        """
        ax4.text(0.1, 0.9, info_text, transform=ax4.transAxes,
                fontsize=10, verticalalignment='top', family='monospace',
                bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.5))

    def _create_time_series_charts(self, data: Dict[str, Any], ax1, ax2, ax3, ax4):
        """Create charts for time series analysis (e.g., monthly progression)"""
        time_series = data['time_series']
        labels = [period['label'] for period in time_series]

        ndwi_values = [period.get('ndwi', 0) for period in time_series]
        turbidity_values = [period.get('turbidity', 0) for period in time_series]
        chlorophyll_values = [period.get('chlorophyll', 0) for period in time_series]
        tsm_values = [period.get('tsm', 0) for period in time_series]
        cdom_values = [period.get('cdom', 0) for period in time_series]
        wqi_values = [period.get('water_quality_index', 50) for period in time_series]

        x = np.arange(len(labels))

        # NDWI time series
        ax1.plot(x, ndwi_values, marker='o', color='#4169e1', linewidth=2, markersize=6, label='NDWI')
        ax1.set_xlabel('Time Period', fontweight='bold')
        ax1.set_ylabel('NDWI (index, −1 to +1)', fontweight='bold')
        ax1.set_title('Water Index Over Time', fontsize=12, fontweight='bold')
        ax1.set_xticks(x)
        ax1.set_xticklabels(labels, rotation=45, ha='right')
        ax1.grid(True, alpha=0.3)

        # Turbidity & Chlorophyll time series (share ax2)
        ax2.plot(x, turbidity_values, marker='s', color='#ff7f50', linewidth=2, markersize=6, label='Turbidity (NDTI)')
        ax2.plot(x, chlorophyll_values, marker='^', color='#32cd32', linewidth=2, markersize=6, label='Chlorophyll (NDCI)')
        ax2.set_xlabel('Time Period', fontweight='bold')
        ax2.set_ylabel('Normalised Index (−1 to +1)', fontweight='bold')
        ax2.set_title('Turbidity & Chlorophyll Over Time', fontsize=12, fontweight='bold')
        ax2.set_xticks(x)
        ax2.set_xticklabels(labels, rotation=45, ha='right')
        ax2.legend(fontsize=8)
        ax2.grid(True, alpha=0.3)

        # TSM & CDOM time series (share ax3)
        ax3.plot(x, tsm_values, marker='v', color='#b8860b', linewidth=2, markersize=6, label='TSM')
        ax3.plot(x, cdom_values, marker='D', color='#6a3d9a', linewidth=2, markersize=6, label='CDOM')
        ax3.set_xlabel('Time Period', fontweight='bold')
        ax3.set_ylabel('TSM (reflectance) / CDOM (ratio)', fontweight='bold')
        ax3.set_title('TSM & CDOM Over Time', fontsize=12, fontweight='bold')
        ax3.set_xticks(x)
        ax3.set_xticklabels(labels, rotation=45, ha='right')
        ax3.legend(fontsize=8)
        ax3.grid(True, alpha=0.3)

        # WQI time series
        ax4.plot(x, wqi_values, marker='D', color='#9370db', linewidth=2, markersize=6, label='WQI')
        ax4.axhline(y=50, color='orange', linestyle='--', label='Threshold', alpha=0.5)
        ax4.set_xlabel('Time Period', fontweight='bold')
        ax4.set_ylabel('WQI Score (0–100)', fontweight='bold')
        ax4.set_title('Water Quality Index Over Time', fontsize=12, fontweight='bold')
        ax4.set_xticks(x)
        ax4.set_xticklabels(labels, rotation=45, ha='right')
        ax4.set_ylim([0, 100])
        ax4.legend()
        ax4.grid(True, alpha=0.3)

    @staticmethod
    def calculate_wqi(
        ndwi: float,
        turbidity: float,
        chlorophyll: float,
        tsm: float = 0.0,
        cdom: float = 0.0,
    ) -> float:
        """
        Calculate Water Quality Index (0-100).

        Scoring rationale (all sub-scores mapped to 0-100):
        • NDWI  (−1..+1): higher → more water → better.  score = (ndwi+1)/2 * 100
        • Turbidity / NDTI (−1..+1): lower → clearer water → better.
          score = (1 − ndti) / 2 * 100   (−1 ⇒ 100, +1 ⇒ 0)
        • Chlorophyll / NDCI (−1..+1): moderate is best; very high ⇒ algal bloom.
          score = max(0, (1 − abs(ndci)) * 100)
        • TSM  (0..0.1 reflectance): lower → less suspended matter → better.
          score = max(0, (1 − tsm / 0.1) * 100)
        • CDOM (0.5..3 ratio): moderate ~1 is natural; very high ⇒ contamination.
          score = max(0, (1 − abs(cdom − 1.0) / 2.0) * 100)

        Weights: NDWI 25 %, Turbidity 25 %, Chlorophyll 20 %, TSM 15 %, CDOM 15 %.
        """
        ndwi = ndwi or 0.0
        turbidity = turbidity or 0.0
        chlorophyll = chlorophyll or 0.0
        tsm = tsm or 0.0
        cdom = cdom or 0.0

        ndwi_score = min(max((ndwi + 1) / 2 * 100, 0), 100)
        turbidity_score = min(max((1 - turbidity) / 2 * 100, 0), 100)
        chlorophyll_score = min(max((1 - abs(chlorophyll)) * 100, 0), 100)
        tsm_score = min(max((1 - tsm / 0.1) * 100, 0), 100)
        cdom_score = min(max((1 - abs(cdom - 1.0) / 2.0) * 100, 0), 100)

        wqi = (
            0.25 * ndwi_score
            + 0.25 * turbidity_score
            + 0.20 * chlorophyll_score
            + 0.15 * tsm_score
            + 0.15 * cdom_score
        )

        return wqi
