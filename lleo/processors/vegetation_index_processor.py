"""
Vegetation Index Processor (NDVI - Normalized Difference Vegetation Index)

Analyzes vegetation health and changes using Sentinel-2 satellite imagery
"""

from typing import List, Dict, Any
import time
import ee
import os
import tempfile

from lleo.core.base_processor import BaseProcessor, AnalysisRequest, AnalysisResult
from lleo.core.config import LLEOConfig
from lleo.core.exceptions import ProcessorException, ValidationException, EarthEngineException
from ..visualization.vegetation_visualization import VegetationVisualization


class VegetationIndexProcessor(BaseProcessor):
    """
    Processor for Vegetation Index (NDVI) analysis

    Capabilities:
    - Vegetation health monitoring
    - Deforestation detection
    - Agricultural monitoring
    - Forest change analysis
    """

    def __init__(self, config: LLEOConfig):
        super().__init__(config)
        self.log_info("VegetationIndexProcessor initialized")

    def get_analysis_type(self) -> str:
        """Return analysis type identifier"""
        return "vegetation_index"

    def get_required_parameters(self) -> List[str]:
        """Return required parameters for Vegetation Index analysis"""
        return ['location', 'time_period']

    def validate_request(self, request: AnalysisRequest) -> bool:
        """
        Validate Vegetation Index analysis request

        Args:
            request: Analysis request

        Returns:
            True if valid

        Raises:
            ValidationException: If validation fails
        """
        # Validate location
        self.validate_location(request.location)

        # Validate time period
        self.validate_time_period(request.time_period)

        # Vegetation Index-specific validations
        if 'start_year' in request.time_period and 'end_year' in request.time_period:
            start_year = request.time_period['start_year']
            end_year = request.time_period['end_year']

            # Sentinel-2 data available from 2015 onwards
            if start_year < 2015:
                raise ValidationException(
                    "Sentinel-2 data is only available from 2015 onwards"
                )

            # Maximum 10-year range for performance
            if end_year - start_year > 10:
                raise ValidationException(
                    "Maximum time range is 10 years for Vegetation Index analysis"
                )

        self.log_info(
            "Request validated successfully",
            session_id=request.session_id,
            location=request.location.get('name'),
            time_range=f"{request.time_period.get('start_year')}-{request.time_period.get('end_year')}"
        )

        return True

    async def process(self, request: AnalysisRequest) -> AnalysisResult:
        """
        Process Vegetation Index analysis

        Args:
            request: Analysis request with validated parameters

        Returns:
            Analysis result with Vegetation Index maps, charts, and data

        Raises:
            ProcessorException: If processing fails
        """
        start_time = time.time()

        try:
            self.log_info(
                "Starting Vegetation Index analysis",
                session_id=request.session_id,
                location=request.location.get('name')
            )

            # Validate request first
            self.validate_request(request)

            # Extract parameters
            lat = request.location['latitude']
            lon = request.location['longitude']
            start_year = request.time_period.get('start_year')
            end_year = request.time_period.get('end_year')

            # Auto-adjust single-year: compare previous year vs requested year
            if start_year and end_year and start_year == end_year:
                self.log_info(
                    f"Single year ({start_year}) detected — comparing {start_year-1} vs {start_year}",
                    session_id=request.session_id,
                )
                start_year = start_year - 1

            # Create Earth Engine geometry
            point = ee.Geometry.Point([lon, lat])
            buffer_size = self.config.default_buffer_size
            region = point.buffer(buffer_size).bounds()

            self.log_info(
                "Created analysis region",
                session_id=request.session_id,
                buffer_size=buffer_size
            )

            # Get Sentinel-2 composites
            start_composite = self._get_sentinel2_composite(
                region,
                f"{start_year}-01-01",
                f"{start_year}-12-31"
            )

            end_composite = self._get_sentinel2_composite(
                region,
                f"{end_year}-01-01",
                f"{end_year}-12-31"
            )

            # Calculate NDVI
            start_ndvi = self._calculate_ndvi(start_composite)
            end_ndvi = self._calculate_ndvi(end_composite)
            ndvi_difference = end_ndvi.subtract(start_ndvi)

            self.log_info(
                "Vegetation Index calculated successfully",
                session_id=request.session_id
            )

            # Classify NDVI and compute statistics
            ndvi_class_start = VegetationVisualization.classify_ndvi(start_ndvi)
            ndvi_class_end = VegetationVisualization.classify_ndvi(end_ndvi)

            statistics = VegetationVisualization.compute_statistics(
                ndvi_class_start,
                ndvi_class_end,
                region,
                start_year,
                end_year
            )

            # Calculate bounds for visualization
            bounds_coords = region.bounds().coordinates().getInfo()[0]
            bounds = {
                'center': [lat, lon],
                'southwest': [bounds_coords[0][1], bounds_coords[0][0]],
                'northeast': [bounds_coords[2][1], bounds_coords[2][0]]
            }

            # Create visualization helper
            viz = VegetationVisualization(
                start_year=start_year,
                end_year=end_year,
                location_name=request.location.get('name', 'Unknown'),
                bounds=bounds
            )

            # Create temporary directory for files
            temp_dir = self.config.temp_dir
            os.makedirs(temp_dir, exist_ok=True)

            map_path = os.path.join(temp_dir, f"{request.session_id}_map.html")
            charts_path = os.path.join(temp_dir, f"{request.session_id}_charts.png")
            preview_path = os.path.join(temp_dir, f"{request.session_id}_charts_preview.png")

            # Generate visualizations
            self.log_info("Generating visualizations...", session_id=request.session_id)

            viz.create_interactive_map(
                mean_ndvi_start=start_ndvi,
                mean_ndvi_end=end_ndvi,
                ndvi_difference=ndvi_difference,
                save_path=map_path
            )

            viz.create_charts(
                statistics=statistics,
                save_path=charts_path
            )

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
                    # Upload preview/thumbnail
                    self.log_info(f"Checking for preview file: {preview_path}", session_id=request.session_id)
                    self.log_info(f"Preview file exists: {os.path.exists(preview_path)}", session_id=request.session_id)
                    if os.path.exists(preview_path):
                        outputs['preview'] = self.file_manager.save_file_to_cloud(
                            preview_path,
                            f"analysis_results/{request.session_id}/charts_preview.png"
                        )
                        self.log_info(f"Charts preview uploaded: {outputs['preview']}", session_id=request.session_id)
                    else:
                        self.log_error(f"Charts preview file not found at {preview_path}", session_id=request.session_id)
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
                'location_name': request.location.get('name', 'Unknown'),
                'buffer_size_m': buffer_size,
                'cloud_cover_threshold': self.config.cloud_cover_threshold,
                'satellite': 'Sentinel-2',
                'analysis_date': time.strftime('%Y-%m-%d %H:%M:%S'),
                'statistics': statistics
            }

            processing_time = time.time() - start_time

            self.log_info(
                "Vegetation Index analysis completed",
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
                f"Unexpected error during Vegetation Index processing: {e}",
                session_id=request.session_id
            )
            raise ProcessorException(f"Vegetation Index processing failed: {e}")

    def _get_sentinel2_composite(
        self,
        region: ee.Geometry,
        start_date: str,
        end_date: str
    ) -> ee.Image:
        """
        Get cloud-masked Sentinel-2 composite for time period

        Args:
            region: Earth Engine geometry
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)

        Returns:
            Earth Engine Image composite
        """
        try:
            collection = (
                ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
                .filterBounds(region)
                .filterDate(start_date, end_date)
                .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', self.config.cloud_cover_threshold))
                .map(self._mask_clouds)
            )

            # Check if collection has images
            count = collection.size().getInfo()
            if count == 0:
                raise EarthEngineException(
                    f"No Sentinel-2 images found for {start_date} to {end_date} "
                    f"with <{self.config.cloud_cover_threshold}% cloud cover"
                )

            self.log_info(f"Found {count} Sentinel-2 images for {start_date} to {end_date}")

            # Return median composite
            return collection.median().clip(region)

        except ee.EEException as e:
            raise EarthEngineException(f"Failed to get Sentinel-2 composite: {e}")

    def _mask_clouds(self, image: ee.Image) -> ee.Image:
        """
        Mask clouds in Sentinel-2 image using QA60 band

        Args:
            image: Sentinel-2 image

        Returns:
            Cloud-masked image
        """
        qa = image.select('QA60')

        # Bits 10 and 11 are clouds and cirrus
        cloud_bit_mask = 1 << 10
        cirrus_bit_mask = 1 << 11

        # Both flags should be zero for clear conditions
        mask = qa.bitwiseAnd(cloud_bit_mask).eq(0).And(
            qa.bitwiseAnd(cirrus_bit_mask).eq(0)
        )

        return image.updateMask(mask)

    def _calculate_ndvi(self, image: ee.Image) -> ee.Image:
        """
        Calculate NDVI from Sentinel-2 image

        Formula: NDVI = (NIR - Red) / (NIR + Red)

        Args:
            image: Sentinel-2 image with B4 (Red) and B8 (NIR) bands

        Returns:
            NDVI image
        """
        nir = image.select('B8')  # Near-Infrared band
        red = image.select('B4')  # Red band

        ndvi = nir.subtract(red).divide(nir.add(red)).rename('NDVI')

        return ndvi

    def get_capabilities(self) -> Dict[str, Any]:
        """
        Return processor capabilities

        Returns:
            Dictionary describing capabilities
        """
        return {
            'analysis_type': self.get_analysis_type(),
            'name': 'Vegetation Index Analysis',
            'description': 'Analyze vegetation health and changes using Sentinel-2 NDVI',
            'satellite': 'Sentinel-2',
            'bands_used': ['B4 (Red)', 'B8 (NIR)'],
            'min_year': 2015,
            'max_time_range_years': 10,
            'spatial_resolution': '100m',
            'temporal_resolution': '5 days',
            'use_cases': [
                'Deforestation monitoring',
                'Agricultural health assessment',
                'Forest change detection',
                'Vegetation mapping',
            ],
            'outputs': [
                'Interactive Vegetation Index map',
                'Time series charts',
                'Satellite thumbnail',
                'Statistical analysis',
                'GeoTIFF data files',
            ]
        }
