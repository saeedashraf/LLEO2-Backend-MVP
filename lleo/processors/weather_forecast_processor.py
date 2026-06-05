"""
Weather Forecast Processor for LLEO
Provides weather forecasting using OpenWeather API
"""

import os
import time
import requests
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Any, List
from ..core.base_processor import BaseProcessor, AnalysisRequest, AnalysisResult
from ..core.exceptions import ValidationException, ProcessorException
from ..visualization.weather_forecast_visualization import WeatherForecastVisualization


class WeatherForecastProcessor(BaseProcessor):
    """
    Weather Forecast Analysis Processor

    Uses OpenWeather API to:
    - Get current weather conditions
    - Provide 5-day forecast
    - Analyze temperature and humidity trends
    """

    def __init__(self, config=None):
        super().__init__(config)

        # API key will be loaded lazily when needed
        self._api_key = None
        self._api_key_loaded = False
        self.base_url = "https://api.openweathermap.org/data/2.5"

        # Timeout for API calls
        self.api_timeout = 60

    @staticmethod
    def _clean_api_key(raw: str) -> str:
        """Strip shell artefacts (-n, quotes, whitespace) that may pollute a secret value."""
        import re
        key = raw.strip()
        # Remove leading "-n " from mis-pasted echo -n "key" commands
        key = re.sub(r'^-n\s+', '', key)
        # Remove surrounding quotes
        key = key.strip('"').strip("'").strip()
        return key

    @property
    def api_key(self) -> str:
        """Lazy-load OpenWeather API key from environment or Secret Manager"""
        if not self._api_key_loaded:
            self._api_key_loaded = True

            # Try environment variable first
            api_key = os.getenv('OPENWEATHER_API_KEY')

            if api_key:
                api_key = self._clean_api_key(api_key)
                print(f"✅ OpenWeather API key loaded from environment (length: {len(api_key)})")
                self._api_key = api_key
                return self._api_key

            # Try getting from Secret Manager if get_secret function is available
            if hasattr(self, 'get_secret') and self.get_secret:
                try:
                    api_key = self.get_secret('openweather-api-key')
                    if api_key:
                        api_key = self._clean_api_key(api_key)
                        print(f"✅ OpenWeather API key loaded from Secret Manager (length: {len(api_key)})")
                        self._api_key = api_key
                        return self._api_key
                except Exception as e:
                    print(f"⚠️  Could not load API key from Secret Manager: {e}")

            print("⚠️  No OpenWeather API key found, will use mock data")
            self._api_key = None

        return self._api_key

    def get_analysis_type(self) -> str:
        return "weather_forecast"

    def get_required_parameters(self) -> List[str]:
        """Return required parameters for weather forecast analysis"""
        return ['location']

    def validate_request(self, request: AnalysisRequest) -> None:
        """Validate weather forecast analysis request"""
        super().validate_request(request)

        # Weather forecast requires location
        if not request.location:
            raise ValidationException("Weather forecast analysis requires a location")

    def process(self, request: AnalysisRequest) -> AnalysisResult:
        """Process weather forecast analysis"""
        start_time = time.time()

        try:
            self.log_info("Starting weather forecast analysis", session_id=request.session_id)

            # Extract location
            location = request.location

            self.log_info(
                f"Fetching weather for: {location.get('name', 'Unknown')}",
                session_id=request.session_id
            )

            # Get weather data
            weather_data = self._get_weather_data(location, request.session_id)

            # Create visualizations
            viz = WeatherForecastVisualization(
                location=location,
                weather_data=weather_data,
                api_key=self.api_key
            )

            # Create temporary directory for files
            temp_dir = self.config.temp_dir
            os.makedirs(temp_dir, exist_ok=True)

            map_path = os.path.join(temp_dir, f"{request.session_id}_map.html")
            charts_path = os.path.join(temp_dir, f"{request.session_id}_charts.png")

            # Generate visualizations
            self.log_info("Generating visualizations...", session_id=request.session_id)

            viz.create_interactive_map(save_path=map_path)
            viz.create_charts(save_path=charts_path)

            # Upload files to storage (if file_manager available)
            outputs = {}
            if hasattr(self, 'file_manager') and self.file_manager:
                try:
                    outputs['map'] = self.file_manager.save_file_to_cloud(
                        map_path,
                        f"analysis_results/{request.session_id}/map.html"
                    )
                    outputs['charts'] = self.file_manager.save_file_to_cloud(
                        charts_path,
                        f"analysis_results/{request.session_id}/charts.png"
                    )
                    self.log_info("Files uploaded to cloud storage", session_id=request.session_id)
                except Exception as e:
                    self.log_error(f"Failed to upload files: {e}", session_id=request.session_id)
                    # Keep local paths as fallback
                    outputs['map'] = map_path
                    outputs['charts'] = charts_path
            else:
                # Use local paths
                outputs['map'] = map_path
                outputs['charts'] = charts_path

            # Calculate metadata
            current_weather = weather_data['current']
            wind = current_weather.get('wind', {})
            rain = current_weather.get('rain', {})
            snow = current_weather.get('snow', {})
            sys_info = current_weather.get('sys', {})
            visibility_m = current_weather.get('visibility')

            metadata = {
                'location_name': location.get('name', 'Unknown'),
                'data_source': 'OpenWeather API',
                'analysis_date': time.strftime('%Y-%m-%d %H:%M:%S'),
                'current_weather': {
                    'temperature': current_weather['main']['temp'],
                    'temp_min': current_weather['main'].get('temp_min'),
                    'temp_max': current_weather['main'].get('temp_max'),
                    'feels_like': current_weather['main']['feels_like'],
                    'humidity': current_weather['main']['humidity'],
                    'pressure': current_weather['main']['pressure'],
                    'conditions': current_weather['weather'][0]['description'],
                    'wind_speed_ms': wind.get('speed', 0),
                    'wind_gust_ms': wind.get('gust'),
                    'wind_direction_deg': wind.get('deg', 0),
                    'clouds_pct': current_weather.get('clouds', {}).get('all', 0),
                    'rain_1h_mm': rain.get('1h', 0),
                    'rain_3h_mm': rain.get('3h', 0),
                    'snow_1h_mm': snow.get('1h', 0),
                    'snow_3h_mm': snow.get('3h', 0),
                    'visibility_km': round(visibility_m / 1000, 1) if visibility_m else None,
                    'sunrise_utc': time.strftime('%H:%M', time.gmtime(sys_info['sunrise'])) if sys_info.get('sunrise') else None,
                    'sunset_utc': time.strftime('%H:%M', time.gmtime(sys_info['sunset'])) if sys_info.get('sunset') else None,
                },
                'forecast_days': 5,
            }

            processing_time = time.time() - start_time

            self.log_info(
                "Weather forecast analysis completed",
                session_id=request.session_id,
                processing_time=f"{processing_time:.2f}s"
            )

            return self.create_result(
                request=request,
                status='completed',
                outputs=outputs,
                metadata=metadata,
                processing_time=processing_time
            )

        except ValidationException as e:
            self.log_error(f"Validation error: {e}", session_id=request.session_id)
            raise

        except Exception as e:
            self.log_error(
                f"Unexpected error during weather forecast processing: {e}",
                session_id=request.session_id
            )
            raise ProcessorException(f"Weather forecast processing failed: {e}")

    def _get_weather_data(self, location: Dict, session_id: str) -> Dict[str, Any]:
        """
        Get weather data from OpenWeather API

        Args:
            location: Location dictionary with lat/lon
            session_id: Session ID for logging

        Returns:
            Dictionary with 'current' and 'forecast' weather data
        """
        if not self.api_key:
            return self._generate_mock_weather()

        # Handle both flattened and nested location formats
        if isinstance(location.get('latitude'), dict):
            lat = location['latitude']['center']
            lon = location['longitude']['center']
        else:
            lat = location['latitude']
            lon = location['longitude']

        try:
            current_url = f"{self.base_url}/weather?lat={lat}&lon={lon}&appid={self.api_key}&units=metric"
            forecast_url = f"{self.base_url}/forecast?lat={lat}&lon={lon}&appid={self.api_key}&units=metric"

            self.log_info(
                f"Fetching current weather from OpenWeather API (lat={lat}, lon={lon})...",
                session_id=session_id,
            )
            current_response = requests.get(current_url, timeout=self.api_timeout)

            self.log_info("Fetching forecast from OpenWeather API...", session_id=session_id)
            forecast_response = requests.get(forecast_url, timeout=self.api_timeout)

            current_response.raise_for_status()
            forecast_response.raise_for_status()

            current_json = current_response.json()
            forecast_json = forecast_response.json()

            # Log the city returned by OpenWeather so we can verify correctness
            api_city = current_json.get("name", "?")
            api_country = current_json.get("sys", {}).get("country", "?")
            self.log_info(
                f"Weather data fetched successfully — API reports city: {api_city}, {api_country}",
                session_id=session_id,
            )

            return {
                'current': current_json,
                'forecast': forecast_json,
            }

        except requests.exceptions.Timeout as e:
            self.log_error(f"Weather API timeout after {self.api_timeout}s: {e}", session_id=session_id)
            return self._generate_mock_weather()
        except requests.exceptions.HTTPError as e:
            self.log_error(f"Weather API HTTP error: {e}", session_id=session_id)
            return self._generate_mock_weather()
        except Exception as e:
            self.log_error(f"Weather API error: {e}", session_id=session_id)
            return self._generate_mock_weather()

    def _generate_mock_weather(self) -> Dict[str, Any]:
        """Generate mock weather data for fallback"""
        print("⚠️  Using mock weather data (API key not available or API error)")

        import time as _time
        now_ts = int(_time.time())

        return {
            'current': {
                'name': 'Mock City',
                'main': {
                    'temp': 22.5,
                    'temp_min': 20.0,
                    'temp_max': 25.0,
                    'feels_like': 21.8,
                    'humidity': 65,
                    'pressure': 1013,
                },
                'weather': [{'main': 'Clear', 'description': 'clear sky'}],
                'wind': {'speed': 3.5, 'deg': 180, 'gust': 5.2},
                'clouds': {'all': 20},
                'rain': {},
                'snow': {},
                'visibility': 10000,
                'sys': {
                    'country': 'XX',
                    'sunrise': now_ts - 3600 * 4,
                    'sunset': now_ts + 3600 * 6,
                },
            },
            'forecast': {
                'list': [
                    {
                        'dt_txt': (datetime.now() + timedelta(hours=i * 3)).strftime('%Y-%m-%d %H:%M:%S'),
                        'main': {
                            'temp': 20 + np.random.random() * 10,
                            'feels_like': 19 + np.random.random() * 10,
                            'humidity': 50 + np.random.random() * 40,
                        },
                        'weather': [{'main': np.random.choice(['Clear', 'Clouds', 'Rain'])}],
                        'wind': {'speed': 2 + np.random.random() * 6, 'deg': int(np.random.random() * 360), 'gust': 4 + np.random.random() * 8},
                        'pop': round(np.random.random() * 0.6, 2),
                        'rain': {'3h': round(np.random.random() * 2, 1)} if np.random.random() > 0.7 else {},
                        'snow': {},
                    }
                    for i in range(40)
                ],
            },
        }

    def get_capabilities(self) -> Dict[str, Any]:
        """Return processor capabilities"""
        return {
            'analysis_type': self.get_analysis_type(),
            'name': 'Weather Forecast',
            'description': 'Get current weather and 5-day forecast using OpenWeather API',
            'data_source': 'OpenWeather API',
            'forecast_days': 5,
            'forecast_interval_hours': 3,
            'units': 'metric',
            'data_provided': [
                'Temperature (current, feels-like, min, max)',
                'Humidity',
                'Pressure',
                'Precipitation (rain & snow, mm)',
                'Precipitation probability',
                'Wind speed, gust & direction',
                'Cloud coverage',
                'Visibility',
                'Sunrise / sunset',
                'Weather conditions',
            ],
            'use_cases': [
                'Weather planning',
                'Agricultural forecasting',
                'Event planning',
                'Travel planning',
            ],
        }
