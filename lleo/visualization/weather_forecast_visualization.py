"""
Weather Forecast Visualization for LLEO
Creates interactive maps and charts for weather forecast analysis.

Visualises **all** variables available from the free OpenWeather
/weather + /forecast endpoints:
  • Temperature (current, feels-like, min/max, daily avg)
  • Humidity
  • Precipitation  – rain.3h, snow.3h, pop (probability of precipitation)
  • Wind speed, gust & direction
  • Pressure, visibility, cloud coverage
  • Sunrise / sunset
  • Weather-condition distribution
"""

import folium
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
from datetime import datetime
from typing import Dict, Any, List, Optional


# ── helpers ──────────────────────────────────────────────────────────────────

def _wind_direction_label(deg: float) -> str:
    """Convert wind degrees to compass direction."""
    dirs = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
            'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
    idx = int((deg + 11.25) / 22.5) % 16
    return dirs[idx]


def _ts_to_time(ts: int, fmt: str = "%H:%M") -> str:
    return datetime.utcfromtimestamp(ts).strftime(fmt)


class WeatherForecastVisualization:
    """Handles visualization for weather forecast analysis"""

    def __init__(self, location: Dict, weather_data: Dict[str, Any], api_key: Optional[str] = None):
        self.location = location
        self.weather_data = weather_data
        self.api_key = api_key

        # Handle both flattened and nested location formats
        if isinstance(location.get('latitude'), dict):
            self.center_lat = location['latitude']['center']
            self.center_lng = location['longitude']['center']
        else:
            self.center_lat = location['latitude']
            self.center_lng = location['longitude']

        self.location_name = location.get('name', 'Unknown')

    # ── Interactive map ──────────────────────────────────────────────────

    def create_interactive_map(self, save_path: str = 'weather_map.html') -> None:
        print("🗺️  Creating interactive weather map...")

        m = folium.Map(location=[self.center_lat, self.center_lng], zoom_start=10)
        folium.TileLayer('OpenStreetMap').add_to(m)

        # Weather tile overlays
        if self.api_key:
            for layer, label in [
                ('temp_new', 'Temperature'),
                ('precipitation_new', 'Precipitation'),
                ('wind_new', 'Wind'),
                ('clouds_new', 'Clouds'),
                ('pressure_new', 'Pressure'),
            ]:
                folium.TileLayer(
                    tiles=f'https://tile.openweathermap.org/map/{layer}/{{{{z}}}}/{{{{x}}}}/{{{{y}}}}.png?appid={self.api_key}',
                    attr='OpenWeatherMap',
                    name=label,
                    overlay=True,
                    control=True,
                ).add_to(m)

        # ── Build rich popup ─────────────────────────────────────────────
        cur = self.weather_data['current']

        # Wind
        wind_speed = cur.get('wind', {}).get('speed', 0)
        wind_gust = cur.get('wind', {}).get('gust')
        wind_deg = cur.get('wind', {}).get('deg', 0)
        wind_dir = _wind_direction_label(wind_deg)
        wind_line = f"💨 Wind: {wind_speed} m/s {wind_dir}"
        if wind_gust:
            wind_line += f" (gust {wind_gust} m/s)"

        # Precipitation
        rain_1h = cur.get('rain', {}).get('1h', 0)
        rain_3h = cur.get('rain', {}).get('3h', 0)
        snow_1h = cur.get('snow', {}).get('1h', 0)
        snow_3h = cur.get('snow', {}).get('3h', 0)
        precip_lines = ""
        if rain_1h or rain_3h:
            precip_lines += f"🌧️ Rain: {rain_1h or rain_3h} mm<br>"
        if snow_1h or snow_3h:
            precip_lines += f"❄️ Snow: {snow_1h or snow_3h} mm<br>"
        if not precip_lines:
            precip_lines = "🌧️ Precipitation: 0 mm<br>"

        # Visibility
        vis_m = cur.get('visibility', None)
        vis_line = f"👁️ Visibility: {vis_m / 1000:.1f} km<br>" if vis_m is not None else ""

        # Sunrise / sunset
        sunrise_ts = cur.get('sys', {}).get('sunrise')
        sunset_ts = cur.get('sys', {}).get('sunset')
        sun_line = ""
        if sunrise_ts and sunset_ts:
            sun_line = (
                f"🌅 Sunrise: {_ts_to_time(sunrise_ts)} UTC &nbsp; "
                f"🌇 Sunset: {_ts_to_time(sunset_ts)} UTC<br>"
            )

        popup_html = f"""
        <div style="font-family:Arial,sans-serif; min-width:240px;">
        <b style="font-size:14px;">Current Weather — {self.location_name}</b><br><hr>
        🌡️ Temperature: {cur['main']['temp']}°C
            (min {cur['main'].get('temp_min', '–')}°C / max {cur['main'].get('temp_max', '–')}°C)<br>
        🤚 Feels Like: {cur['main']['feels_like']}°C<br>
        💧 Humidity: {cur['main']['humidity']}%<br>
        🔽 Pressure: {cur['main']['pressure']} hPa<br>
        {wind_line}<br>
        ☁️ Clouds: {cur['clouds']['all']}%<br>
        {precip_lines}
        {vis_line}
        {sun_line}
        <b>Conditions:</b> {cur['weather'][0]['description'].title()}
        </div>
        """

        folium.Marker(
            [self.center_lat, self.center_lng],
            popup=folium.Popup(popup_html, max_width=350),
            icon=folium.Icon(color='orange', icon='cloud'),
        ).add_to(m)

        if self.api_key:
            m.add_child(folium.LayerControl())

        m.save(save_path)
        print(f"✅ Interactive map saved to {save_path}")

    # ── Charts (6-panel) ─────────────────────────────────────────────────

    def create_charts(self, save_path: str = 'weather_charts.png') -> None:
        print("📊 Creating weather forecast charts (6 panels)...")

        fig, axes = plt.subplots(3, 2, figsize=(16, 14))
        fig.suptitle(
            f'Weather Forecast Dashboard — {self.location_name}',
            fontsize=17, fontweight='bold', y=0.98,
        )

        forecast_list: List[Dict] = self.weather_data['forecast']['list']
        first48 = forecast_list[:16]  # 16 × 3 h = 48 h

        times = [f['dt_txt'].split(' ')[1][:5] for f in first48]
        x_idx = np.arange(len(times))

        temps      = [f['main']['temp'] for f in first48]
        feels_like = [f['main'].get('feels_like', f['main']['temp']) for f in first48]
        humidity   = [f['main']['humidity'] for f in first48]
        # rain/snow in mm per 3 h; pop = 0-1 probability
        rain       = [f.get('rain', {}).get('3h', 0) for f in first48]
        snow       = [f.get('snow', {}).get('3h', 0) for f in first48]
        pop        = [f.get('pop', 0) * 100 for f in first48]  # → percent
        wind_speed = [f.get('wind', {}).get('speed', 0) for f in first48]
        wind_gust  = [f.get('wind', {}).get('gust', 0) for f in first48]

        # ── 1  Temperature + feels-like ──────────────────────────────────
        ax = axes[0, 0]
        ax.plot(x_idx, temps, 'r-', linewidth=2, marker='o', markersize=4, label='Temperature')
        ax.plot(x_idx, feels_like, 'r--', linewidth=1, alpha=0.6, label='Feels like')
        ax.fill_between(x_idx, temps, alpha=0.15, color='red')
        ax.set_xticks(x_idx)
        ax.set_xticklabels(times, rotation=45, fontsize=8)
        ax.set_ylabel('°C', fontweight='bold')
        ax.set_title('48-Hour Temperature Forecast', fontsize=12, fontweight='bold')
        min_t, max_t = min(temps), max(temps)
        ax.axhline(y=min_t, color='blue', ls='--', alpha=.4, label=f'Min {min_t:.1f}°C')
        ax.axhline(y=max_t, color='red', ls='--', alpha=.4, label=f'Max {max_t:.1f}°C')
        ax.legend(loc='upper right', fontsize=8)
        ax.grid(True, alpha=0.3)

        # ── 2  Humidity ──────────────────────────────────────────────────
        ax = axes[0, 1]
        ax.bar(x_idx, humidity, color='#4682B4', alpha=0.7)
        ax.set_xticks(x_idx)
        ax.set_xticklabels(times, rotation=45, fontsize=8)
        ax.set_ylabel('Humidity (%)', fontweight='bold')
        ax.set_title('48-Hour Humidity Forecast', fontsize=12, fontweight='bold')
        ax.set_ylim(0, 100)
        ax.grid(True, alpha=0.3, axis='y')

        # ── 3  Precipitation + probability ───────────────────────────────
        ax = axes[1, 0]
        total_precip = [r + s for r, s in zip(rain, snow)]
        bar_colors = ['#87CEEB' if s == 0 else '#B0E0E6' for s in snow]
        bars = ax.bar(x_idx, total_precip, color=bar_colors, alpha=0.8, label='Rain + Snow (mm)')
        # Overlay snow portion
        if any(s > 0 for s in snow):
            ax.bar(x_idx, snow, color='#E0E8FF', alpha=0.9, label='Snow (mm)')
        ax.set_xticks(x_idx)
        ax.set_xticklabels(times, rotation=45, fontsize=8)
        ax.set_ylabel('Precipitation (mm / 3h)', fontweight='bold', color='#4682B4')
        ax.set_title('48-Hour Precipitation & Probability', fontsize=12, fontweight='bold')
        ax.grid(True, alpha=0.3, axis='y')

        # Probability on secondary axis
        ax2 = ax.twinx()
        ax2.plot(x_idx, pop, 'o-', color='#FF8C00', linewidth=1.5, markersize=4, alpha=0.8, label='Prob. %')
        ax2.set_ylabel('Probability (%)', fontweight='bold', color='#FF8C00')
        ax2.set_ylim(0, 105)
        ax2.tick_params(axis='y', labelcolor='#FF8C00')

        # Combined legend
        lines1, labels1 = ax.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax.legend(lines1 + lines2, labels1 + labels2, loc='upper right', fontsize=8)

        # ── 4  Wind speed + gust ─────────────────────────────────────────
        ax = axes[1, 1]
        ax.plot(x_idx, wind_speed, 'g-', linewidth=2, marker='o', markersize=4, label='Speed')
        if any(g > 0 for g in wind_gust):
            ax.plot(x_idx, wind_gust, 'g--', linewidth=1, alpha=0.6, marker='^', markersize=3, label='Gust')
            ax.fill_between(x_idx, wind_speed, wind_gust, alpha=0.10, color='green')
        ax.set_xticks(x_idx)
        ax.set_xticklabels(times, rotation=45, fontsize=8)
        ax.set_ylabel('m/s', fontweight='bold')
        ax.set_title('48-Hour Wind Forecast', fontsize=12, fontweight='bold')
        ax.legend(loc='upper right', fontsize=8)
        ax.grid(True, alpha=0.3)

        # ── 5  Weather conditions distribution (pie) ─────────────────────
        ax = axes[2, 0]
        conditions: Dict[str, int] = {}
        for f in forecast_list:
            cond = f['weather'][0]['main']
            conditions[cond] = conditions.get(cond, 0) + 1

        if conditions:
            cmap = {
                'Clear': '#FFD700', 'Clouds': '#B0C4DE', 'Rain': '#4682B4',
                'Snow': '#E0E8FF', 'Drizzle': '#87CEEB', 'Thunderstorm': '#483D8B',
                'Mist': '#D3D3D3', 'Fog': '#C0C0C0', 'Haze': '#DEB887',
            }
            colors = [cmap.get(k, '#808080') for k in conditions]
            ax.pie(conditions.values(), labels=conditions.keys(),
                   autopct='%1.0f%%', colors=colors, startangle=90, textprops={'fontsize': 9})
            ax.set_title('5-Day Conditions Distribution', fontsize=12, fontweight='bold')
        else:
            ax.text(0.5, 0.5, 'No data', ha='center', va='center', fontsize=14)
            ax.set_title('5-Day Conditions', fontsize=12, fontweight='bold')

        # ── 6  Daily summary bars (avg temp + total precip) ──────────────
        ax = axes[2, 1]
        daily_temps: List[float] = []
        daily_precip: List[float] = []
        daily_labels: List[str] = []

        for i in range(0, min(40, len(forecast_list)), 8):
            day = forecast_list[i:i + 8]
            if not day:
                continue
            daily_temps.append(float(np.mean([d['main']['temp'] for d in day])))
            daily_precip.append(
                sum(d.get('rain', {}).get('3h', 0) + d.get('snow', {}).get('3h', 0) for d in day)
            )
            ds = day[0]['dt_txt'].split(' ')[0]
            parts = ds.split('-')
            daily_labels.append(f"{parts[2]}/{parts[1]}" if len(parts) == 3 else f"Day {len(daily_temps)}")

        dx = np.arange(len(daily_labels))
        width = 0.38

        if daily_temps:
            # Temp bars
            tbars = ax.bar(dx - width / 2, daily_temps, width, label='Avg Temp (°C)', edgecolor='black')
            for bar, t in zip(tbars, daily_temps):
                bar.set_color('#4169E1' if t < 10 else '#FFA500' if t < 20 else '#FF4500')
                ax.text(bar.get_x() + bar.get_width() / 2, t + 0.3, f'{t:.1f}°', ha='center', fontsize=8, fontweight='bold')

            # Precip bars on secondary axis
            ax_p = ax.twinx()
            pbars = ax_p.bar(dx + width / 2, daily_precip, width, color='#4682B4', alpha=0.6, label='Total Precip (mm)')
            ax_p.set_ylabel('Precipitation (mm)', fontweight='bold', color='#4682B4')
            ax_p.tick_params(axis='y', labelcolor='#4682B4')
            for bar, p in zip(pbars, daily_precip):
                if p > 0:
                    ax_p.text(bar.get_x() + bar.get_width() / 2, p + 0.1, f'{p:.1f}', ha='center', fontsize=8, color='#4682B4')

            ax.set_xticks(dx)
            ax.set_xticklabels(daily_labels)
            ax.set_ylabel('Temperature (°C)', fontweight='bold')
            ax.set_title('5-Day Daily Summary', fontsize=12, fontweight='bold')
            ax.grid(True, alpha=0.3, axis='y')

            # Combined legend
            h1, l1 = ax.get_legend_handles_labels()
            h2, l2 = ax_p.get_legend_handles_labels()
            ax.legend(h1 + h2, l1 + l2, loc='upper right', fontsize=8)
        else:
            ax.text(0.5, 0.5, 'No daily data', ha='center', va='center', fontsize=14)
            ax.set_title('5-Day Daily Summary', fontsize=12, fontweight='bold')

        plt.tight_layout(rect=[0, 0, 1, 0.96])
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
        plt.close()

        print(f"✅ Charts saved to {save_path}")
