"""
Air Quality Processor for LLEO
Analyzes air pollution using Sentinel-5P satellite data
"""

import os
import time
import ee
from typing import Dict, Any, List
from ..core.base_processor import BaseProcessor, AnalysisRequest, AnalysisResult
from ..core.exceptions import ValidationException, EarthEngineException, ProcessorException
from ..visualization.air_quality_visualization import AirQualityVisualization


class AirQualityProcessor(BaseProcessor):
    """
    Air Quality Analysis Processor

    Uses Sentinel-5P TROPOMI data to analyze atmospheric pollutants:
    - NO2 (Nitrogen Dioxide)
    - SO2 (Sulfur Dioxide)
    - CO (Carbon Monoxide)
    - O3 (Ozone)
    - CH4 (Methane)
    """

    def __init__(self, config=None):
        super().__init__(config)

        # Pollutant configurations
        self.pollutants = {
            'NO2': {
                'collection': 'COPERNICUS/S5P/OFFL/L3_NO2',
                'band': 'tropospheric_NO2_column_number_density',
                'unit': 'mol/m²',
                'scale': 1e6,
                'label': 'NO₂ Concentration'
            },
            'SO2': {
                'collection': 'COPERNICUS/S5P/OFFL/L3_SO2',
                'band': 'SO2_column_number_density',
                'unit': 'mol/m²',
                'scale': 1e6,
                'label': 'SO₂ Concentration'
            },
            'CO': {
                'collection': 'COPERNICUS/S5P/OFFL/L3_CO',
                'band': 'CO_column_number_density',
                'unit': 'mol/m²',
                'scale': 1e4,
                'label': 'CO Concentration'
            },
            'O3': {
                'collection': 'COPERNICUS/S5P/OFFL/L3_O3',
                'band': 'O3_column_number_density',
                'unit': 'mol/m²',
                'scale': 1e3,
                'label': 'O₃ Concentration'
            },
            'CH4': {
                'collection': 'COPERNICUS/S5P/OFFL/L3_CH4',
                'band': 'CH4_column_volume_mixing_ratio_dry_air',
                'unit': 'ppb',
                'scale': 1,
                'label': 'CH₄ Concentration'
            }
        }

    def get_analysis_type(self) -> str:
        return "air_quality"

    def get_required_parameters(self) -> List[str]:
        """Return required parameters for air quality analysis"""
        return ['location', 'time_period']

    def validate_request(self, request: AnalysisRequest) -> None:
        """Validate air quality analysis request"""
        super().validate_request(request)

        # Air quality analysis requires location
        if not request.location:
            raise ValidationException("Air quality analysis requires a location")

        # Sentinel-5P data available from 2018 onwards
        time_period = request.time_period or {}
        start_year = time_period.get('start_year', 2020)

        if start_year < 2018:
            self.log_warning(
                "Sentinel-5P data only available from 2018 onwards. Adjusting start year to 2018.",
                session_id=request.session_id
            )

    def process(self, request: AnalysisRequest) -> AnalysisResult:
        """Process air quality analysis"""
        start_time = time.time()

        try:
            self.log_info("Starting air quality analysis", session_id=request.session_id)

            # Extract parameters
            location = request.location
            time_period = request.time_period or {}

            start_year = max(2018, time_period.get('start_year', 2020))
            end_year = time_period.get('end_year', 2024)

            start_date = f"{start_year}-01-01"
            end_date = f"{end_year}-12-31"

            self.log_info(
                f"Analysis period: {start_date} to {end_date}",
                session_id=request.session_id
            )

            # Create region geometry for air quality
            # Handle both flattened and nested location formats
            if isinstance(location.get('latitude'), dict):
                center_lat = location['latitude']['center']
                center_lon = location['longitude']['center']
            else:
                center_lat = location['latitude']
                center_lon = location['longitude']

            # Reduced buffer for faster processing (0.2 degrees ~ 22km)
            buffer_size = 0.2
            region = ee.Geometry.Rectangle([
                center_lon - buffer_size,
                center_lat - buffer_size,
                center_lon + buffer_size,
                center_lat + buffer_size
            ])

            bounds = {
                'center': (center_lat, center_lon),
                'southwest': (center_lat - buffer_size, center_lon - buffer_size),
                'northeast': (center_lat + buffer_size, center_lon + buffer_size)
            }

            self.log_info(
                f"Analysis area: center ({center_lat:.4f}, {center_lon:.4f}), buffer {buffer_size}°",
                session_id=request.session_id
            )

            # Determine which pollutants to process
            selected = request.parameters.get('pollutants') or request.parameters.get('selected_parameters')
            if selected and isinstance(selected, list) and len(selected) > 0:
                # Normalise to uppercase for matching
                selected_upper = [p.upper() for p in selected]
                active_pollutants = {
                    k: v for k, v in self.pollutants.items() if k.upper() in selected_upper
                }
                if not active_pollutants:
                    self.log_warning(
                        f"No matching pollutants for {selected}, falling back to all",
                        session_id=request.session_id,
                    )
                    active_pollutants = self.pollutants
                else:
                    self.log_info(
                        f"Analyzing selected pollutants: {list(active_pollutants.keys())}",
                        session_id=request.session_id,
                    )
            else:
                active_pollutants = self.pollutants
                self.log_info(
                    "No specific pollutants selected — analyzing all 5",
                    session_id=request.session_id,
                )

            # Process pollutant data
            pollutant_results, pollutant_images = self._process_pollutants(
                region, start_date, end_date, request.session_id,
                pollutant_configs=active_pollutants,
            )

            # Create visualizations
            viz = AirQualityVisualization(
                bounds=bounds,
                start_date=start_date,
                end_date=end_date,
                location_name=location.get('name', 'Unknown'),
                pollutant_images=pollutant_images
            )

            # Create temporary directory for files
            temp_dir = self.config.temp_dir
            os.makedirs(temp_dir, exist_ok=True)

            map_path = os.path.join(temp_dir, f"{request.session_id}_map.html")
            charts_path = os.path.join(temp_dir, f"{request.session_id}_charts.png")

            # Generate visualizations
            self.log_info("Generating visualizations...", session_id=request.session_id)

            viz.create_interactive_map(save_path=map_path)
            viz.create_charts(data=pollutant_results, save_path=charts_path)

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
            metadata = {
                'start_year': start_year,
                'end_year': end_year,
                'location_name': location.get('name', 'Unknown'),
                'buffer_size_deg': buffer_size,
                'satellite': 'Sentinel-5P',
                'analysis_date': time.strftime('%Y-%m-%d %H:%M:%S'),
                'pollutant_data': pollutant_results
            }

            processing_time = time.time() - start_time

            self.log_info(
                "Air quality analysis completed",
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

        except ee.EEException as e:
            self.log_error(f"Earth Engine error: {e}", session_id=request.session_id)
            raise EarthEngineException(f"Earth Engine error: {e}")

        except Exception as e:
            self.log_error(
                f"Unexpected error during air quality processing: {e}",
                session_id=request.session_id
            )
            raise ProcessorException(f"Air quality processing failed: {e}")

    def _process_pollutants(self, region: ee.Geometry, start_date: str, end_date: str, session_id: str, pollutant_configs: dict = None) -> tuple:
        """
        Process selected pollutants for the given region and time period

        Args:
            pollutant_configs: Dict of pollutant name → config to process.
                               Defaults to self.pollutants (all 5) if None.

        Returns:
            Tuple of (pollutant_results, pollutant_images)
        """
        if pollutant_configs is None:
            pollutant_configs = self.pollutants

        results = {}
        images = {}

        self.log_info(
            f"Processing {len(pollutant_configs)} pollutant(s) from Sentinel-5P "
            f"({', '.join(pollutant_configs.keys())}) — monthly composites over "
            f"{start_date} .. {end_date}",
            session_id=session_id
        )

        # Number of whole months spanned by the request.
        ee_start, ee_end = ee.Date(start_date), ee.Date(end_date)
        n_months = int(ee_end.difference(ee_start, 'month').ceil().getInfo())
        n_months = max(1, n_months)

        for pollutant_name, config in pollutant_configs.items():
            try:
                self.log_info(f"Processing {pollutant_name}...", session_id=session_id)

                band = config['band']
                scale_factor = config['scale']

                base_collection = (
                    ee.ImageCollection(config['collection'])
                    .filterBounds(region)
                    .filterDate(start_date, end_date)
                    .select(band)
                )

                total_size = base_collection.size().getInfo()

                if total_size == 0:
                    self.log_warning(f"No images found for {pollutant_name}", session_id=session_id)
                    results[pollutant_name] = {
                        'value': None,
                        'unit': config['unit'],
                        'label': config['label'],
                        'image_count': 0,
                    }
                    continue

                # Mean over the WHOLE period.
                #
                # This previously used .sort('system:time_start').limit(36),
                # which takes the 36 EARLIEST images — for a 20-month request
                # that was the first ~3 days only, and for sparsely-retrieved
                # bands like CH4 every pixel in that window is masked, so the
                # regional mean came back None and the map tiles were empty.
                # .mean() is a server-side reduction, so there is no need to
                # cap the image count.
                mean_image = base_collection.mean().clip(region)
                images[pollutant_name] = mean_image

                mean_value = mean_image.reduceRegion(
                    reducer=ee.Reducer.mean(),
                    geometry=region,
                    scale=5500,
                    maxPixels=1e9
                ).get(band).getInfo()

                results[pollutant_name] = {
                    'value': mean_value * scale_factor if mean_value is not None else None,
                    'unit': config['unit'],
                    'label': config['label'],
                    'image_count': total_size,
                }

                if mean_value is None:
                    self.log_warning(
                        f"{pollutant_name}: {total_size} images matched but every pixel in "
                        f"the region is masked (no valid retrievals)",
                        session_id=session_id
                    )
                else:
                    print(f"      ✅ {pollutant_name}: {mean_value * scale_factor:.2f} {config['unit']} "
                          f"(from {total_size} images)")

                # ── Real monthly series, fetched in a single round trip ──
                try:
                    def _monthly(i):
                        m_start = ee_start.advance(ee.Number(i), 'month')
                        m_end = m_start.advance(1, 'month')
                        val = (
                            base_collection
                            .filterDate(m_start, m_end)
                            .mean()
                            .reduceRegion(
                                reducer=ee.Reducer.mean(),
                                geometry=region,
                                scale=5500,
                                maxPixels=1e9
                            ).get(band)
                        )
                        return ee.Feature(None, {
                            'label': m_start.format('YYYY-MM'),
                            'value': val,
                        })

                    rows = ee.FeatureCollection(
                        ee.List.sequence(0, n_months - 1).map(_monthly)
                    ).getInfo()['features']

                    series = [
                        (r['properties'].get('label'), r['properties'].get('value'))
                        for r in rows
                    ]
                    results[pollutant_name]['monthly'] = [
                        {'label': lbl, 'value': (v * scale_factor) if v is not None else None}
                        for lbl, v in series
                    ]
                    valid = sum(1 for _, v in series if v is not None)
                    self.log_info(
                        f"{pollutant_name}: {valid}/{n_months} months with valid data",
                        session_id=session_id
                    )
                except Exception as series_err:
                    self.log_warning(
                        f"{pollutant_name}: monthly series unavailable: {series_err}",
                        session_id=session_id
                    )
                    results[pollutant_name]['monthly'] = []

            except Exception as e:
                self.log_error(f"Error processing {pollutant_name}: {e}", session_id=session_id)
                results[pollutant_name] = {
                    'value': None,
                    'unit': config['unit'],
                    'label': config['label'],
                    'image_count': 0,
                    'monthly': [],
                }

        return results, images

    def get_capabilities(self) -> Dict[str, Any]:
        """Return processor capabilities"""
        return {
            'analysis_type': self.get_analysis_type(),
            'name': 'Air Quality Analysis',
            'description': 'Analyze atmospheric pollutants using Sentinel-5P TROPOMI data',
            'satellite': 'Sentinel-5P',
            'pollutants': ['NO2', 'SO2', 'CO', 'O3', 'CH4'],
            'min_year': 2018,
            'max_time_range_years': 10,
            'spatial_resolution': '5.5km',
            'temporal_resolution': 'Daily',
            'use_cases': [
                'Urban air pollution monitoring',
                'Industrial emission tracking',
                'Environmental impact assessment',
                'Public health studies'
            ]
        }
