"""
Water Quality Processor

Analyzes water quality using Sentinel-2 satellite imagery
Measures NDWI, turbidity, and chlorophyll concentration
"""

from typing import List, Dict, Any
import time
import ee
import os

from lleo.core.base_processor import BaseProcessor, AnalysisRequest, AnalysisResult
from lleo.core.config import LLEOConfig
from lleo.core.exceptions import ProcessorException, ValidationException, EarthEngineException
from ..visualization.water_quality_visualization import WaterQualityVisualization
from lleo.routing.analysis_planner import plan_analysis_strategy


class WaterQualityProcessor(BaseProcessor):
    """
    Processor for water quality analysis

    Capabilities:
    - Water body health monitoring
    - NDWI (Normalized Difference Water Index)
    - Turbidity measurement
    - Chlorophyll concentration
    - Water Quality Index calculation
    """

    def __init__(self, config: LLEOConfig):
        super().__init__(config)
        self.model = None  # Vertex AI model for intelligent planning
        self.log_info("WaterQualityProcessor initialized")

    def get_analysis_type(self) -> str:
        """Return analysis type identifier"""
        return "water_quality"

    def get_required_parameters(self) -> List[str]:
        """Return required parameters for water quality analysis"""
        return ['location', 'time_period']

    def validate_request(self, request: AnalysisRequest) -> bool:
        """
        Validate water quality analysis request

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

        # Water quality specific validations
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
                    "Maximum time range is 10 years for water quality analysis"
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
        Process water quality analysis

        Args:
            request: Analysis request with validated parameters

        Returns:
            Analysis result with water quality maps, charts, and data

        Raises:
            ProcessorException: If processing fails
        """
        start_time = time.time()

        try:
            self.log_info(
                "Starting water quality analysis",
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

            # Create Earth Engine geometry
            point = ee.Geometry.Point([lon, lat])
            buffer_size = self.config.default_buffer_size
            region = point.buffer(buffer_size).bounds()

            self.log_info(
                "Created analysis region",
                session_id=request.session_id,
                buffer_size=buffer_size
            )

            # Use intelligent analysis planner to determine optimal strategy
            analysis_strategy = None
            if self.model:
                try:
                    self.log_info("🎯 Using AI to plan analysis strategy...", session_id=request.session_id)
                    analysis_strategy = await plan_analysis_strategy(
                        query=request.query,
                        analysis_type=self.get_analysis_type(),
                        location=request.location.get('name', 'Unknown'),
                        time_period=request.time_period,
                        model=self.model
                    )
                    self.log_info(
                        f"📋 Strategy: {analysis_strategy.get('strategy_type')}, "
                        f"{len(analysis_strategy.get('time_periods', []))} periods",
                        session_id=request.session_id
                    )
                except Exception as e:
                    self.log_error(f"Analysis planning failed, using fallback: {e}", session_id=request.session_id)
                    analysis_strategy = None

            # Process based on strategy
            if analysis_strategy and analysis_strategy.get('time_periods'):
                # AI-guided analysis
                self.log_info("🤖 Processing with AI-guided strategy", session_id=request.session_id)

                # Process each time period
                period_results = []
                for period in analysis_strategy['time_periods']:
                    start_date = period['date_range']['start']
                    end_date = period['date_range']['end']
                    label = period.get('label', start_date)

                    self.log_info(f"Processing period: {label}", session_id=request.session_id)
                    period_data = self._process_date_range(region, start_date, end_date, label)
                    period_results.append(period_data)

                # Build water quality data based on comparison type
                comparison_type = analysis_strategy.get('comparison', {}).get('type', 'single_period')

                if comparison_type == 'two_period' and len(period_results) >= 2:
                    # Two period comparison
                    period1_data = period_results[0]
                    period2_data = period_results[1]

                    water_quality_data = {
                        'ndwi': period2_data['ndwi'],
                        'turbidity': period2_data['turbidity'],
                        'chlorophyll': period2_data['chlorophyll'],
                        'water_quality_index': period2_data['water_quality_index'],
                        'comparison': {
                            period1_data['label']: period1_data,
                            period2_data['label']: period2_data
                        },
                        'strategy': analysis_strategy
                    }

                    # Use most recent images for visualization
                    ndwi_image = period2_data.get('ndwi_image')
                    turbidity_image = period2_data.get('turbidity_image')
                    chlorophyll_image = period2_data.get('chlorophyll_image')
                    tsm_image = period2_data.get('tsm_image')
                    cdom_image = period2_data.get('cdom_image')
                    true_color_image = period2_data.get('true_color_image')

                elif comparison_type == 'time_series':
                    # Time series analysis
                    water_quality_data = {
                        'time_series': period_results,
                        'strategy': analysis_strategy
                    }

                    # Use latest period that actually has images for map visualisation
                    latest = period_results[-1]  # default fallback
                    for pr in reversed(period_results):
                        if pr.get('ndwi_image') is not None:
                            latest = pr
                            break

                    ndwi_image = latest.get('ndwi_image')
                    turbidity_image = latest.get('turbidity_image')
                    chlorophyll_image = latest.get('chlorophyll_image')
                    tsm_image = latest.get('tsm_image')
                    cdom_image = latest.get('cdom_image')
                    true_color_image = latest.get('true_color_image')
                    water_quality_data.update({
                        'ndwi': latest['ndwi'],
                        'turbidity': latest['turbidity'],
                        'chlorophyll': latest['chlorophyll'],
                        'water_quality_index': latest['water_quality_index']
                    })
                else:
                    # Single period
                    period_data = period_results[0] if period_results else {}
                    water_quality_data = period_data.copy()
                    water_quality_data['strategy'] = analysis_strategy

                    ndwi_image = period_data.get('ndwi_image')
                    turbidity_image = period_data.get('turbidity_image')
                    chlorophyll_image = period_data.get('chlorophyll_image')
                    tsm_image = period_data.get('tsm_image')
                    cdom_image = period_data.get('cdom_image')
                    true_color_image = period_data.get('true_color_image')

            else:
                # Fallback: simple year comparison logic
                self.log_info("⚙️  Using fallback year comparison logic", session_id=request.session_id)

                if start_year != end_year:
                    # Year comparison analysis
                    self.log_info(f"Processing year comparison: {start_year} vs {end_year}")

                    year1_data = self._process_year_data(region, start_year, start_year)
                    year2_data = self._process_year_data(region, end_year, end_year)

                    water_quality_data = {
                        'ndwi': year1_data['ndwi'],
                        'turbidity': year1_data['turbidity'],
                        'chlorophyll': year1_data['chlorophyll'],
                        'water_quality_index': year1_data['water_quality_index'],
                        'year_comparison': {
                            str(start_year): year1_data,
                            str(end_year): year2_data
                        }
                    }

                    # Use year2 images for visualization (most recent)
                    ndwi_image = year2_data.get('ndwi_image')
                    turbidity_image = year2_data.get('turbidity_image')
                    chlorophyll_image = year2_data.get('chlorophyll_image')
                    tsm_image = year2_data.get('tsm_image')
                    cdom_image = year2_data.get('cdom_image')
                    true_color_image = year2_data.get('true_color_image')
                else:
                    # Single year analysis
                    self.log_info(f"Processing single year: {start_year}")

                    water_quality_data = self._process_year_data(region, start_year, start_year)

                    ndwi_image = water_quality_data.get('ndwi_image')
                    turbidity_image = water_quality_data.get('turbidity_image')
                    chlorophyll_image = water_quality_data.get('chlorophyll_image')
                    tsm_image = water_quality_data.get('tsm_image')
                    cdom_image = water_quality_data.get('cdom_image')
                    true_color_image = water_quality_data.get('true_color_image')

            self.log_info(
                "Water quality data processed successfully",
                session_id=request.session_id
            )

            # Determine which indicators were selected by the user
            selected_indicators = request.parameters.get('water_indicators') or request.parameters.get('selected_parameters')
            if selected_indicators and isinstance(selected_indicators, list) and len(selected_indicators) > 0:
                selected_lower = [s.lower() for s in selected_indicators]
                self.log_info(
                    f"User selected water indicators: {selected_indicators}",
                    session_id=request.session_id,
                )
            else:
                selected_lower = ['ndwi', 'turbidity', 'chlorophyll', 'tsm', 'cdom']
                self.log_info(
                    "No specific indicators selected — showing all 5",
                    session_id=request.session_id,
                )

            # Calculate bounds for visualization
            bounds_coords = region.bounds().coordinates().getInfo()[0]
            bounds = {
                'center': [lat, lon],
                'southwest': [bounds_coords[0][1], bounds_coords[0][0]],
                'northeast': [bounds_coords[2][1], bounds_coords[2][0]]
            }

            # Create visualization helper
            viz = WaterQualityVisualization(
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

            # Generate visualizations — pass only selected indicators' images
            self.log_info("Generating visualizations...", session_id=request.session_id)

            viz.create_interactive_map(
                true_color_image=true_color_image,
                ndwi_image=ndwi_image if 'ndwi' in selected_lower else None,
                turbidity_image=turbidity_image if 'turbidity' in selected_lower else None,
                chlorophyll_image=chlorophyll_image if 'chlorophyll' in selected_lower else None,
                tsm_image=tsm_image if 'tsm' in selected_lower else None,
                cdom_image=cdom_image if 'cdom' in selected_lower else None,
                save_path=map_path
            )

            viz.create_charts(
                data=water_quality_data,
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
                    if os.path.exists(preview_path):
                        outputs['preview'] = self.file_manager.save_file_to_cloud(
                            preview_path,
                            f"analysis_results/{request.session_id}/charts_preview.png"
                        )
                    self.log_info("Files uploaded to cloud storage (including preview)", session_id=request.session_id)
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
                'satellite': 'Sentinel-2',
                'selected_indicators': selected_indicators if selected_indicators else ['NDWI', 'turbidity', 'chlorophyll'],
                'analysis_date': time.strftime('%Y-%m-%d %H:%M:%S'),
                'water_quality_data': water_quality_data
            }

            processing_time = time.time() - start_time

            self.log_info(
                "Water quality analysis completed",
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
                f"Unexpected error during water quality processing: {e}",
                session_id=request.session_id
            )
            raise ProcessorException(f"Water quality processing failed: {e}")

    def _process_date_range(self, region: ee.Geometry, start_date: str, end_date: str, label: str = None) -> Dict[str, Any]:
        """
        Process water quality data for a specific date range (e.g., specific month or season)

        Args:
            region: Earth Engine geometry
            start_date: Start date in YYYY-MM-DD format
            end_date: End date in YYYY-MM-DD format
            label: Human-readable label for this period (e.g., "January 2023")

        Returns:
            Dictionary with water quality data and images
        """
        max_images = 36
        label = label or f"{start_date} to {end_date}"

        print(f"   Processing data for {label}: {start_date} to {end_date}")

        # Load base Sentinel-2 collection
        base_collection = (
            ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
            .filterBounds(region)
            .filterDate(start_date, end_date)
            .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20))
        )

        total_size = base_collection.size().getInfo()
        print(f"      Total available: {total_size} images")

        if total_size == 0:
            print(f"      ⚠️  No images found for {label}")
            return {
                'label': label,
                'ndwi': 0.1,
                'turbidity': 0.0,
                'chlorophyll': 0.0,
                'tsm': 0.0,
                'cdom': 0.0,
                'water_quality_index': 50.0,
                'ndwi_image': None,
                'turbidity_image': None,
                'chlorophyll_image': None,
                'tsm_image': None,
                'cdom_image': None,
                'true_color_image': None
            }

        if total_size <= max_images:
            collection = base_collection
            print(f"      ✅ Using all {total_size} images")
        else:
            # Sort by cloud coverage to get clearest images
            collection = base_collection.sort('CLOUDY_PIXEL_PERCENTAGE').limit(max_images)
            print(f"      ✅ Using {max_images} clearest images (from {total_size})")

        # Apply water quality calculations
        collection = (
            collection
            .map(self._calculate_ndwi)
            .map(self._calculate_turbidity)
            .map(self._calculate_chlorophyll)
            .map(self._calculate_tsm)
            .map(self._calculate_cdom)
        )

        size = collection.size().getInfo()
        print(f"      Processing {size} images for water quality indices")

        # Calculate mean images for visualization
        ndwi_image = collection.select('NDWI').mean().clip(region)
        turbidity_image = collection.select('Turbidity').mean().clip(region)
        chlorophyll_image = collection.select('Chlorophyll').mean().clip(region)
        tsm_image = collection.select('TSM').mean().clip(region)
        cdom_image = collection.select('CDOM').mean().clip(region)
        true_color_image = collection.select(['B4', 'B3', 'B2']).mean().clip(region)

        # Calculate statistics
        mean_ndwi = ndwi_image.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=region,
            scale=500,
            maxPixels=1e9
        ).get('NDWI').getInfo()

        mean_turbidity = turbidity_image.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=region,
            scale=500,
            maxPixels=1e9
        ).get('Turbidity').getInfo()

        mean_chlorophyll = chlorophyll_image.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=region,
            scale=500,
            maxPixels=1e9
        ).get('Chlorophyll').getInfo()

        mean_tsm = tsm_image.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=region,
            scale=500,
            maxPixels=1e9
        ).get('TSM').getInfo()

        mean_cdom = cdom_image.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=region,
            scale=500,
            maxPixels=1e9
        ).get('CDOM').getInfo()

        print(f"   ✅ {label} - NDWI: {mean_ndwi:.3f}")
        print(f"   ✅ {label} - Turbidity (NDTI): {mean_turbidity:.3f}")
        print(f"   ✅ {label} - Chlorophyll (NDCI): {mean_chlorophyll:.3f}")
        print(f"   ✅ {label} - TSM: {mean_tsm:.4f}")
        print(f"   ✅ {label} - CDOM: {mean_cdom:.3f}")

        # Calculate Water Quality Index
        wqi = WaterQualityVisualization.calculate_wqi(mean_ndwi, mean_turbidity, mean_chlorophyll, mean_tsm, mean_cdom)

        return {
            'label': label,
            'ndwi': mean_ndwi or 0,
            'turbidity': mean_turbidity or 0,
            'chlorophyll': mean_chlorophyll or 0,
            'tsm': mean_tsm or 0,
            'cdom': mean_cdom or 0,
            'water_quality_index': wqi,
            'ndwi_image': ndwi_image,
            'turbidity_image': turbidity_image,
            'chlorophyll_image': chlorophyll_image,
            'tsm_image': tsm_image,
            'cdom_image': cdom_image,
            'true_color_image': true_color_image
        }

    def _process_year_data(self, region: ee.Geometry, start_year: int, end_year: int) -> Dict[str, Any]:
        """
        Process water quality data for a specific year

        Args:
            region: Earth Engine geometry
            start_year: Start year
            end_year: End year

        Returns:
            Dictionary with water quality data and images
        """
        start_date = f"{start_year}-01-01"
        end_date = f"{end_year}-12-31"
        max_images = 36

        print(f"   Processing data for {start_year}: {start_date} to {end_date}")

        # Load base Sentinel-2 collection
        base_collection = (
            ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
            .filterBounds(region)
            .filterDate(start_date, end_date)
            .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20))
        )

        total_size = base_collection.size().getInfo()
        print(f"      Total available: {total_size} images")

        if total_size == 0:
            print(f"      ⚠️  No images found for {start_year}")
            return {
                'ndwi': 0.1,
                'turbidity': 0.0,
                'chlorophyll': 0.0,
                'tsm': 0.0,
                'cdom': 0.0,
                'water_quality_index': 50.0,
                'ndwi_image': None,
                'turbidity_image': None,
                'chlorophyll_image': None,
                'tsm_image': None,
                'cdom_image': None,
                'true_color_image': None
            }

        if total_size <= max_images:
            collection = base_collection
            print(f"      ✅ Using all {total_size} images")
        else:
            collection = base_collection.sort('CLOUDY_PIXEL_PERCENTAGE').limit(max_images)
            print(f"      ✅ Using {max_images} clearest images (from {total_size})")

        # Apply water quality calculations
        collection = (
            collection
            .map(self._calculate_ndwi)
            .map(self._calculate_turbidity)
            .map(self._calculate_chlorophyll)
            .map(self._calculate_tsm)
            .map(self._calculate_cdom)
        )

        size = collection.size().getInfo()
        print(f"      Processing {size} images for water quality indices")

        # Calculate mean images for visualization
        ndwi_image = collection.select('NDWI').mean().clip(region)
        turbidity_image = collection.select('Turbidity').mean().clip(region)
        chlorophyll_image = collection.select('Chlorophyll').mean().clip(region)
        tsm_image = collection.select('TSM').mean().clip(region)
        cdom_image = collection.select('CDOM').mean().clip(region)
        true_color_image = collection.select(['B4', 'B3', 'B2']).mean().clip(region)

        # Calculate statistics
        mean_ndwi = ndwi_image.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=region, scale=500, maxPixels=1e9
        ).get('NDWI').getInfo()

        mean_turbidity = turbidity_image.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=region, scale=500, maxPixels=1e9
        ).get('Turbidity').getInfo()

        mean_chlorophyll = chlorophyll_image.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=region, scale=500, maxPixels=1e9
        ).get('Chlorophyll').getInfo()

        mean_tsm = tsm_image.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=region, scale=500, maxPixels=1e9
        ).get('TSM').getInfo()

        mean_cdom = cdom_image.reduceRegion(
            reducer=ee.Reducer.mean(), geometry=region, scale=500, maxPixels=1e9
        ).get('CDOM').getInfo()

        print(f"   ✅ {start_year} - NDWI: {mean_ndwi:.3f}")
        print(f"   ✅ {start_year} - Turbidity (NDTI): {mean_turbidity:.3f}")
        print(f"   ✅ {start_year} - Chlorophyll (NDCI): {mean_chlorophyll:.3f}")
        print(f"   ✅ {start_year} - TSM: {mean_tsm:.4f}")
        print(f"   ✅ {start_year} - CDOM: {mean_cdom:.3f}")

        # Calculate Water Quality Index
        wqi = WaterQualityVisualization.calculate_wqi(mean_ndwi, mean_turbidity, mean_chlorophyll, mean_tsm, mean_cdom)

        return {
            'ndwi': mean_ndwi or 0,
            'turbidity': mean_turbidity or 0,
            'chlorophyll': mean_chlorophyll or 0,
            'tsm': mean_tsm or 0,
            'cdom': mean_cdom or 0,
            'water_quality_index': wqi,
            'ndwi_image': ndwi_image,
            'turbidity_image': turbidity_image,
            'chlorophyll_image': chlorophyll_image,
            'tsm_image': tsm_image,
            'cdom_image': cdom_image,
            'true_color_image': true_color_image
        }

    def _calculate_ndwi(self, image: ee.Image) -> ee.Image:
        """
        Normalized Difference Water Index (McFeeters 1996)
        Detects water bodies and their extent.

        Formula: NDWI = (Green − NIR) / (Green + NIR)
        Range: −1 to +1  (>0 = water)
        """
        ndwi = image.normalizedDifference(['B3', 'B8']).rename('NDWI')
        return image.addBands(ndwi)

    def _calculate_turbidity(self, image: ee.Image) -> ee.Image:
        """
        Normalized Difference Turbidity Index (Lacaux et al. 2007)
        Measures water clarity / suspended sediment.

        Formula: NDTI = (Red − Green) / (Red + Green)
        Range: −1 to +1  (higher = more turbid)
        """
        ndti = image.normalizedDifference(['B4', 'B3']).rename('Turbidity')
        return image.addBands(ndti)

    def _calculate_chlorophyll(self, image: ee.Image) -> ee.Image:
        """
        Normalized Difference Chlorophyll Index (Mishra & Mishra 2012)
        Estimates chlorophyll-a concentration using Red Edge band.

        Formula: NDCI = (RedEdge − Red) / (RedEdge + Red)
        Uses B5 (705 nm) which is specifically designed for vegetation/chl-a in water.
        Range: −1 to +1  (higher = more chlorophyll / algae)
        """
        ndci = image.normalizedDifference(['B5', 'B4']).rename('Chlorophyll')
        return image.addBands(ndci)

    def _calculate_tsm(self, image: ee.Image) -> ee.Image:
        """
        Total Suspended Matter proxy using Red Edge reflectance.
        Higher B5 reflectance correlates with sediment load.

        Formula: TSM ≈ B5 / 10000  (scaled reflectance)
        """
        tsm = image.select('B5').divide(10000).rename('TSM')
        return image.addBands(tsm)

    def _calculate_cdom(self, image: ee.Image) -> ee.Image:
        """
        Colored Dissolved Organic Matter proxy.
        CDOM absorbs blue light, so low B2/B3 ratio → high CDOM.

        Formula: CDOM_index = Green / Blue  (inverted: low values = high CDOM)
        """
        cdom = image.select('B3').divide(image.select('B2')).rename('CDOM')
        return image.addBands(cdom)

    def get_capabilities(self) -> Dict[str, Any]:
        """
        Return processor capabilities

        Returns:
            Dictionary describing capabilities
        """
        return {
            'analysis_type': self.get_analysis_type(),
            'name': 'Water Quality Analysis',
            'description': 'Analyze water body health using Sentinel-2 NDWI, turbidity, and chlorophyll',
            'satellite': 'Sentinel-2',
            'bands_used': ['B2 (Blue)', 'B3 (Green)', 'B4 (Red)', 'B8 (NIR)'],
            'min_year': 2015,
            'max_time_range_years': 10,
            'spatial_resolution': '500m',  # Optimized for faster processing
            'temporal_resolution': '5 days',
            'use_cases': [
                'Lake/river water quality monitoring',
                'Water pollution detection',
                'Algae bloom monitoring',
                'Water clarity assessment',
            ],
            'outputs': [
                'Interactive water quality map',
                'Time series charts',
                'NDWI, turbidity, chlorophyll layers',
                'Water Quality Index (WQI)',
                'Statistical analysis',
            ]
        }
