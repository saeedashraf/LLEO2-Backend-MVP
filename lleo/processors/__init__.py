"""
Domain-specific processors for different analysis types
"""

from typing import Dict, Type
from lleo.core.base_processor import BaseProcessor

# Import all processors
from .vegetation_index_processor import VegetationIndexProcessor
from .water_quality_processor import WaterQualityProcessor
from .air_quality_processor import AirQualityProcessor
from .business_location_processor import BusinessLocationProcessor
from .weather_forecast_processor import WeatherForecastProcessor
from .video_animation_processor import VideoAnimationProcessor

# Processor registry - maps analysis_type string to processor class
PROCESSORS: Dict[str, Type[BaseProcessor]] = {
    'vegetation_index': VegetationIndexProcessor,
    'water_quality': WaterQualityProcessor,
    'air_quality': AirQualityProcessor,
    'business_location': BusinessLocationProcessor,
    'weather_forecast': WeatherForecastProcessor,
    'video_animation': VideoAnimationProcessor,
}


def get_processor(analysis_type: str) -> Type[BaseProcessor]:
    """
    Get processor class by analysis type

    Args:
        analysis_type: Analysis type identifier (e.g., 'ndvi', 'air_quality')

    Returns:
        Processor class

    Raises:
        ValueError: If analysis type is not registered
    """
    if analysis_type not in PROCESSORS:
        available = ', '.join(PROCESSORS.keys())
        raise ValueError(
            f"Unknown analysis type: '{analysis_type}'. "
            f"Available types: {available}"
        )
    return PROCESSORS[analysis_type]


def register_processor(analysis_type: str, processor_class: Type[BaseProcessor]):
    """
    Register a new processor (for plugins/extensions)

    Args:
        analysis_type: Analysis type identifier
        processor_class: Processor class (must inherit from BaseProcessor)

    Raises:
        TypeError: If processor_class doesn't inherit from BaseProcessor
    """
    if not issubclass(processor_class, BaseProcessor):
        raise TypeError(
            f"Processor class must inherit from BaseProcessor, "
            f"got {processor_class}"
        )

    PROCESSORS[analysis_type] = processor_class
    print(f"✅ Registered processor: {analysis_type} -> {processor_class.__name__}")


def list_processors() -> Dict[str, Type[BaseProcessor]]:
    """
    Get all registered processors

    Returns:
        Dictionary mapping analysis types to processor classes
    """
    return PROCESSORS.copy()


__all__ = [
    'get_processor',
    'register_processor',
    'list_processors',
    'PROCESSORS',
    'VegetationIndexProcessor',
    'WaterQualityProcessor',
    'AirQualityProcessor',
    'BusinessLocationProcessor',
    'WeatherForecastProcessor',
]
